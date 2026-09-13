from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

import app.room_termination as room_termination_module
import app.security as security_module
from app.api.voice import _lock_call_completion_records
from app.config import get_settings
from app.db import SessionLocal
from app.livekit_admin import terminate_livekit_rooms
from app.models import (
    CallSession,
    LiveKitRoomTerminationJob,
    PostCallResult,
    UsageCounter,
    UsageEvent,
)
from tests.test_control_plane import activate, issue, register, signed_worker_post


@pytest.fixture(autouse=True)
def clear_in_memory_auth_rate_limit() -> None:
    with security_module._AUTH_RATE_LOCK:
        security_module._AUTH_ATTEMPTS.clear()


def _active_platform_call(
    tenant: TestClient,
    superadmin: TestClient,
    origin: dict[str, str],
) -> tuple[int, int, int, str]:
    workspace_id = register(tenant, origin, "revocation@example.com", "Revocation tenant")
    entitlement = issue(superadmin, origin, workspace_id, "platform")
    activate(tenant, origin, workspace_id, entitlement["licenseKey"])
    headers = origin | {"x-workspace-id": str(workspace_id)}
    agent_response = tenant.post(
        "/api/agents",
        headers=headers,
        json={
            "name": "Revocation voice agent",
            "maxCallSeconds": 120,
            "workflow": {"nodes": [], "edges": []},
        },
    )
    assert agent_response.status_code == 201, agent_response.text
    agent_id = int(agent_response.json()["agent"]["id"])
    token_response = tenant.post(
        "/api/livekit/token",
        headers=headers,
        json={"agentId": agent_id, "sessionId": "revocationtest"},
    )
    assert token_response.status_code == 200, token_response.text
    room_name = token_response.json()["room_name"]
    config_response = signed_worker_post(
        tenant,
        "/api/internal/agents/config",
        {"roomName": room_name},
    )
    assert config_response.status_code == 200, config_response.text
    with SessionLocal.begin() as db:
        call = db.scalar(select(CallSession).where(CallSession.room_name == room_name))
        assert call is not None and call.status == "active"
        details = dict(call.details)
        details["postCallPlan"] = [
            {"id": "qa-after-call", "type": "QA", "config": {"rubric": "Pass only if resolved"}},
            {"id": "webhook-after-call", "type": "Webhook", "config": {}},
        ]
        call.details = details
        call_id = call.id
    return workspace_id, int(entitlement["license"]["id"]), call_id, room_name


def test_revoke_atomically_enqueues_room_termination_and_late_report_is_safe(
    tenant: TestClient,
    superadmin: TestClient,
    origin: dict[str, str],
) -> None:
    workspace_id, license_id, call_id, room_name = _active_platform_call(tenant, superadmin, origin)
    revoke = superadmin.post(
        f"/api/superadmin/licenses/{license_id}/revoke",
        headers=origin,
        json={"reason": "security shutdown"},
    )
    assert revoke.status_code == 200, revoke.text

    with SessionLocal() as db:
        canceled = db.get(CallSession, call_id)
        assert canceled is not None and canceled.status == "canceled"
        canceled_at = canceled.ended_at
        termination = db.scalar(
            select(LiveKitRoomTerminationJob).where(LiveKitRoomTerminationJob.call_id == call_id)
        )
        assert termination is not None
        assert termination.room_name == room_name
        assert termination.reason_code == "license_revoked"
        assert termination.status == "pending"

    late_payload = {
        "roomName": room_name,
        "durationSeconds": 12,
        "tokensUsed": 64,
        "creditsUsed": 75,
        "costMicros": 900,
        "transferCount": 1,
        "transcript": "must not be persisted after cancellation",
        "summary": "must not trigger QA",
        "sentiment": "positive",
        "disposition": "RESOLVED",
        "pipelineCompleted": True,
        "recordingKey": f"calls/w{workspace_id}/late.wav",
        "safetyEvents": ["rail.output.handoff"],
    }
    first = signed_worker_post(tenant, "/api/internal/calls/complete", late_payload)
    assert first.status_code == 200, first.text
    assert first.json() == {
        "completed": True,
        "canceled": True,
        "callId": call_id,
        "idempotent": False,
        "postCall": {"evaluations": 0, "webhooks": 0, "failures": 0, "pending": 0},
    }
    second = signed_worker_post(
        tenant,
        "/api/internal/calls/complete",
        {**late_payload, "durationSeconds": 119, "tokensUsed": 1_900},
    )
    assert second.status_code == 200, second.text
    assert second.json()["idempotent"] is True

    with SessionLocal() as db:
        call = db.get(CallSession, call_id)
        assert call is not None and call.status == "canceled"
        assert call.ended_at == canceled_at
        assert call.duration_seconds == 12
        assert call.credits_used == 75 and call.cost_micros == 900
        assert call.transfer_count == 1
        assert call.transcript == "" and call.summary == ""
        assert call.sentiment is None and call.disposition is None
        assert call.pipeline_completed is False and call.recording_key is None
        assert call.details["lateCompletion"]["durationSeconds"] == 12
        assert call.details["lateCompletion"]["tokensUsed"] == 64
        assert db.scalar(select(func.count(PostCallResult.id)).where(PostCallResult.call_id == call_id)) == 0
        voice_counter = db.scalar(
            select(UsageCounter.used).where(
                UsageCounter.license_id == license_id,
                UsageCounter.unit == "voice_seconds",
            )
        )
        token_counter = db.scalar(
            select(UsageCounter.used).where(
                UsageCounter.license_id == license_id,
                UsageCounter.unit == "tokens",
            )
        )
        assert voice_counter == 12 and token_counter == 64
        voice_events = db.scalar(
            select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                UsageEvent.license_id == license_id,
                UsageEvent.kind == "voice_seconds",
            )
        )
        token_events = db.scalar(
            select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                UsageEvent.license_id == license_id,
                UsageEvent.kind == "tokens",
            )
        )
        assert voice_events == 12 and token_events == 64


def test_canceled_call_cannot_report_more_usage_than_was_precharged(
    tenant: TestClient,
    superadmin: TestClient,
    origin: dict[str, str],
) -> None:
    _workspace_id, license_id, call_id, room_name = _active_platform_call(tenant, superadmin, origin)

    response = superadmin.post(
        f"/api/superadmin/licenses/{license_id}/revoke",
        headers=origin,
        json={"reason": "usage bound test"},
    )
    assert response.status_code == 200
    too_much_voice = signed_worker_post(
        tenant,
        "/api/internal/calls/complete",
        {"roomName": room_name, "durationSeconds": 121, "tokensUsed": 1},
    )
    assert too_much_voice.status_code == 422
    too_many_tokens = signed_worker_post(
        tenant,
        "/api/internal/calls/complete",
        {"roomName": room_name, "durationSeconds": 1, "tokensUsed": 1_921},
    )
    assert too_many_tokens.status_code == 422
    with SessionLocal() as db:
        call = db.get(CallSession, call_id)
        assert call is not None and call.status == "canceled"
        assert "lateCompletion" not in call.details
        assert (
            db.scalar(
                select(UsageCounter.used).where(
                    UsageCounter.license_id == license_id,
                    UsageCounter.unit == "voice_seconds",
                )
            )
            == 120
        )
        assert (
            db.scalar(
                select(UsageCounter.used).where(
                    UsageCounter.license_id == license_id,
                    UsageCounter.unit == "tokens",
                )
            )
            == 1_920
        )


@pytest.mark.asyncio
async def test_livekit_termination_is_deduplicated_validated_and_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from livekit import api

    deleted: list[str] = []
    clients = []

    class FakeRoomService:
        async def delete_room(self, request) -> None:
            deleted.append(request.room)
            if request.room.endswith("-failure"):
                raise RuntimeError("simulated LiveKit failure")

    class FakeClient:
        def __init__(self, url: str, api_key: str, api_secret: str) -> None:
            self.url = url
            self.api_key = api_key
            self.api_secret = api_secret
            self.room = FakeRoomService()
            self.closed = False
            clients.append(self)

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(api, "LiveKitAPI", FakeClient)
    await terminate_livekit_rooms(
        [
            "test-w1-a1-validroom",
            "test-w1-a1-validroom",
            "test-w1-a1-failure",
            "../unsafe-room",
        ],
        get_settings(),
    )
    assert deleted == ["test-w1-a1-validroom", "test-w1-a1-failure"]
    assert len(clients) == 2
    assert all(client.url == "http://livekit:7880" for client in clients)
    assert all(client.closed is True for client in clients)


def test_room_termination_outbox_retries_indefinitely_and_can_recover(
    tenant: TestClient,
    superadmin: TestClient,
    origin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace_id, license_id, call_id, _room_name = _active_platform_call(tenant, superadmin, origin)
    revoked = superadmin.post(
        f"/api/superadmin/licenses/{license_id}/revoke",
        headers=origin,
        json={"reason": "durable retry test"},
    )
    assert revoked.status_code == 200, revoked.text

    async def fail_delete(_room_name, _settings) -> None:
        raise RuntimeError("temporary failure")

    monkeypatch.setattr(room_termination_module, "terminate_livekit_room", fail_delete)
    for _ in range(8):
        with SessionLocal.begin() as db:
            job = db.scalar(
                select(LiveKitRoomTerminationJob).where(LiveKitRoomTerminationJob.call_id == call_id)
            )
            assert job is not None
            job.next_attempt_at = security_module.now_utc()
        assert room_termination_module.run_pending_room_terminations(get_settings()) == 1

    with SessionLocal() as db:
        job = db.scalar(select(LiveKitRoomTerminationJob).where(LiveKitRoomTerminationJob.call_id == call_id))
        assert job is not None and job.status == "pending"
        assert job.attempts == 8 and job.alerted_at is not None

    async def succeed_delete(_room_name, _settings) -> None:
        return None

    monkeypatch.setattr(room_termination_module, "terminate_livekit_room", succeed_delete)
    with SessionLocal.begin() as db:
        job = db.scalar(select(LiveKitRoomTerminationJob).where(LiveKitRoomTerminationJob.call_id == call_id))
        assert job is not None
        job.next_attempt_at = security_module.now_utc()
    assert room_termination_module.run_pending_room_terminations(get_settings()) == 1
    with SessionLocal() as db:
        job = db.scalar(select(LiveKitRoomTerminationJob).where(LiveKitRoomTerminationJob.call_id == call_id))
        assert job is not None and job.status == "succeeded"


def test_completion_lock_order_remains_workspace_then_license_then_call() -> None:
    statements = []

    class RecordingSession:
        def __init__(self) -> None:
            self.responses = iter(
                [
                    SimpleNamespace(id=1),
                    SimpleNamespace(id=2, workspace_id=1),
                    SimpleNamespace(id=3, license_id=2),
                ]
            )

        def scalar(self, statement):
            statements.append(statement)
            return next(self.responses)

    call, license_row = _lock_call_completion_records(
        RecordingSession(),  # type: ignore[arg-type]
        workspace_id=1,
        agent_id=4,
        room_name="test-w1-a4-lockorder",
    )
    assert call.id == 3 and license_row.id == 2
    rendered = [str(statement.compile(dialect=postgresql.dialect())) for statement in statements]
    assert "FROM workspaces" in rendered[0] and "FOR UPDATE OF workspaces" in rendered[0]
    assert "FROM licenses JOIN call_sessions" in rendered[1] and "FOR UPDATE OF licenses" in rendered[1]
    assert "FROM call_sessions" in rendered[2] and "FOR UPDATE OF call_sessions" in rendered[2]
