from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import CallSession


def register(client, origin):
    response = client.post(
        "/api/auth/register",
        headers=origin,
        json={
            "email": "race@example.com",
            "password": "Tenant-password-123!",
            "name": "Owner",
            "workspaceName": "Race tenant",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["workspaceId"]


def test_revoke_during_dispatch_prevents_sip_dial(tenant, superadmin, origin, monkeypatch):
    workspace_id = register(tenant, origin)
    now = datetime.now(UTC)
    issued = superadmin.post(
        "/api/superadmin/licenses",
        headers=origin,
        json={
            "workspaceId": workspace_id,
            "plan": "hybrid",
            "seats": 1,
            "validFrom": (now - timedelta(minutes=1)).isoformat(),
            "validUntil": (now + timedelta(days=1)).isoformat(),
            "providerMode": "hybrid",
            "hybridPolicy": {
                "llm": "platform",
                "stt": "platform",
                "tts": "platform",
                "realtime": "platform",
                "telephony": "byok",
            },
            "quotas": {"agents": 2, "tokens": 10000, "voice_seconds": 1000},
            "features": ["agents", "providers", "telephony", "voice"],
        },
    )
    assert issued.status_code == 201, issued.text
    license_id = issued.json()["license"]["id"]
    headers = origin | {"x-workspace-id": str(workspace_id)}
    activated = tenant.post(
        "/api/licenses/activate",
        headers=headers,
        json={"licenseKey": issued.json()["licenseKey"]},
    )
    assert activated.status_code == 200, activated.text
    telephony = tenant.post(
        "/api/telephony",
        headers=headers,
        json={
            "provider": "twilio",
            "credentials": {"accountSid": "AC123456789", "authToken": "secret-token-value"},
            "config": {"callerIds": "+14155550100"},
        },
    )
    assert telephony.status_code == 201, telephony.text
    agent = tenant.post(
        "/api/agents",
        headers=headers,
        json={"name": "Race", "status": "published", "maxCallSeconds": 300},
    )
    assert agent.status_code == 201, agent.text
    agent_id = agent.json()["agent"]["id"]
    to_number = "+14155550123"
    consent = tenant.post(
        "/api/compliance",
        headers=headers,
        json={
            "type": "consent",
            "phoneNumber": to_number,
            "status": "granted",
            "legalBasis": "consent",
            "evidenceRef": "test",
        },
    )
    assert consent.status_code == 201, consent.text

    dispatch_started = threading.Event()
    release_dispatch = threading.Event()
    sip_dialed = threading.Event()

    class FakeDispatch:
        async def create_dispatch(self, _request):
            dispatch_started.set()
            await asyncio.to_thread(release_dispatch.wait, 5)

    class FakeSip:
        async def create_sip_participant(self, _request):
            sip_dialed.set()
            return SimpleNamespace(participant_id="p", sip_call_id="c")

    class FakeRoom:
        async def delete_room(self, _request):
            return None

    class FakeLiveKit:
        def __init__(self, *_args, **_kwargs):
            self.agent_dispatch = FakeDispatch()
            self.sip = FakeSip()
            self.room = FakeRoom()

        async def aclose(self):
            return None

    from livekit import api as livekit_api

    settings = get_settings()
    monkeypatch.setattr(settings, "OUTBOUND_SIP_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "OUTBOUND_SIP_TRUNK_MAP_JSON",
        json.dumps({str(workspace_id): {"twilio": "ST_server_owned"}}),
    )
    monkeypatch.setattr(livekit_api, "LiveKitAPI", FakeLiveKit)

    result = {}

    def call():
        result["response"] = tenant.post(
            "/api/calls", headers=headers, json={"agentId": agent_id, "toNumber": to_number}
        )

    thread = threading.Thread(target=call)
    thread.start()
    assert dispatch_started.wait(5)
    with SessionLocal() as db:
        queued = db.scalar(select(CallSession).where(CallSession.workspace_id == workspace_id))
        assert queued is not None and queued.status == "queued"
    revoked = superadmin.post(
        f"/api/superadmin/licenses/{license_id}/revoke",
        headers=origin,
        json={"reason": "race proof"},
    )
    assert revoked.status_code == 200, revoked.text
    release_dispatch.set()
    thread.join(5)
    assert not thread.is_alive()
    assert not sip_dialed.is_set(), "SIP was dialed after the license had been revoked"
    assert result["response"].status_code == 402, result["response"].text
    with SessionLocal() as db:
        canceled = db.scalar(select(CallSession).where(CallSession.workspace_id == workspace_id))
        assert canceled is not None and canceled.status == "canceled"
