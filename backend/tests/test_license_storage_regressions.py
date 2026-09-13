from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

import app.api.licenses as licenses_api
import app.licensing as licensing_module
import app.security as security_module
import app.storage as storage_module
from app.api.dashboard import _livekit_service_fields
from app.api.voice import _assert_public_livekit_url, _lock_call_completion_records
from app.config import get_settings
from app.db import SessionLocal
from app.licensing import active_license_for_workspace
from app.models import CallSession, License, StoredFile, Workspace
from tests.test_control_plane import activate, issue, register


@pytest.fixture(autouse=True)
def clear_in_memory_auth_rate_limit() -> None:
    # The whole test suite shares one process/client IP while production clients
    # do not; keep unrelated fixture logins from exhausting that process-global bucket.
    with security_module._AUTH_RATE_LOCK:
        security_module._AUTH_ATTEMPTS.clear()


def _create_agent(client: TestClient, origin: dict[str, str], workspace_id: int) -> int:
    response = client.post(
        "/api/agents",
        headers=origin | {"x-workspace-id": str(workspace_id)},
        json={"name": "Lifecycle agent", "workflow": {"nodes": [], "edges": []}},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["agent"]["id"])


def test_newer_nonactive_license_never_shadows_active_and_revoke_is_scoped(
    tenant: TestClient,
    superadmin: TestClient,
    origin: dict[str, str],
) -> None:
    workspace_id = register(tenant, origin, "lifecycle@example.com", "Lifecycle tenant")
    first = issue(superadmin, origin, workspace_id)
    activate(tenant, origin, workspace_id, first["licenseKey"])
    first_id = int(first["license"]["id"])
    agent_id = _create_agent(tenant, origin, workspace_id)

    second = issue(superadmin, origin, workspace_id)
    second_id = int(second["license"]["id"])
    headers = origin | {"x-workspace-id": str(workspace_id)}
    assert tenant.get("/api/licenses/current", headers=headers).json()["license"]["id"] == first_id

    current = datetime.now(UTC)
    with SessionLocal.begin() as db:
        db.add_all(
            [
                CallSession(
                    workspace_id=workspace_id,
                    license_id=first_id,
                    agent_id=agent_id,
                    room_name=f"test-w{workspace_id}-a{agent_id}-firstqueued",
                    status="queued",
                    reserved_voice_seconds=120,
                    reserved_tokens=240,
                    reservation_expires_at=current + timedelta(minutes=5),
                ),
                CallSession(
                    workspace_id=workspace_id,
                    license_id=first_id,
                    agent_id=agent_id,
                    room_name=f"test-w{workspace_id}-a{agent_id}-firstactive",
                    status="active",
                    reserved_voice_seconds=120,
                    reserved_tokens=240,
                    reservation_expires_at=current + timedelta(minutes=5),
                ),
                CallSession(
                    workspace_id=workspace_id,
                    license_id=second_id,
                    agent_id=agent_id,
                    room_name=f"test-w{workspace_id}-a{agent_id}-secondqueued",
                    status="queued",
                    reserved_voice_seconds=120,
                    reserved_tokens=240,
                    reservation_expires_at=current + timedelta(minutes=5),
                ),
            ]
        )

    revoked_unused = superadmin.post(
        f"/api/superadmin/licenses/{second_id}/revoke",
        headers=origin,
        json={"reason": "superseded before activation"},
    )
    assert revoked_unused.status_code == 200, revoked_unused.text
    assert tenant.get("/api/licenses/current", headers=headers).json()["license"]["id"] == first_id
    assert tenant.get("/api/agents", headers=headers).status_code == 200
    with SessionLocal() as db:
        workspace = db.get(Workspace, workspace_id)
        rows = db.scalars(select(CallSession).order_by(CallSession.id)).all()
        assert workspace is not None and workspace.status == "active"
        assert [row.status for row in rows] == ["queued", "active", "queued"]

    revoked_active = superadmin.post(
        f"/api/superadmin/licenses/{first_id}/revoke",
        headers=origin,
        json={"reason": "tenant license revoked"},
    )
    assert revoked_active.status_code == 200, revoked_active.text
    assert tenant.get("/api/agents", headers=headers).status_code == 402
    with SessionLocal() as db:
        workspace = db.get(Workspace, workspace_id)
        rows = db.scalars(select(CallSession).order_by(CallSession.id)).all()
        assert workspace is not None and workspace.status == "pending_license"
        assert [row.status for row in rows] == ["canceled", "canceled", "queued"]
        assert all(row.reservation_expires_at is None for row in rows[:2])
        assert all(row.reserved_voice_seconds == 0 and row.reserved_tokens == 0 for row in rows[:2])


def test_expired_active_license_can_be_replaced_by_renewal(
    tenant: TestClient,
    superadmin: TestClient,
    origin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = register(tenant, origin, "renewal@example.com", "Renewal tenant")
    first = issue(superadmin, origin, workspace_id)
    activate(tenant, origin, workspace_id, first["licenseKey"])
    first_id = int(first["license"]["id"])

    current = datetime.now(UTC)
    renewal_response = superadmin.post(
        "/api/superadmin/licenses",
        headers=origin,
        json={
            "workspaceId": workspace_id,
            "plan": "renewal",
            "seats": 5,
            "validFrom": (current - timedelta(minutes=1)).isoformat(),
            "validUntil": (current + timedelta(days=365)).isoformat(),
            "providerMode": "byok",
            "quotas": {"agents": 10},
            "features": ["agents", "members", "providers"],
        },
    )
    assert renewal_response.status_code == 201, renewal_response.text
    renewal = renewal_response.json()
    renewal_id = int(renewal["license"]["id"])

    after_first_expiry = current + timedelta(days=31)
    monkeypatch.setattr(licensing_module, "now_utc", lambda: after_first_expiry)
    monkeypatch.setattr(licenses_api, "now_utc", lambda: after_first_expiry)
    activate(tenant, origin, workspace_id, renewal["licenseKey"])

    with SessionLocal() as db:
        first_row = db.get(License, first_id)
        renewal_row = db.get(License, renewal_id)
        assert first_row is not None and first_row.status == "expired"
        assert renewal_row is not None and renewal_row.status == "active"
        assert active_license_for_workspace(db, workspace_id, get_settings()).license.id == renewal_id


def test_database_only_license_state_tampering_is_rejected(
    tenant: TestClient,
    superadmin: TestClient,
    origin: dict[str, str],
) -> None:
    workspace_id = register(tenant, origin, "state-tamper@example.com", "State tamper tenant")
    issued = issue(superadmin, origin, workspace_id)
    license_id = int(issued["license"]["id"])
    headers = origin | {"x-workspace-id": str(workspace_id)}

    with SessionLocal.begin() as db:
        row = db.get(License, license_id)
        assert row is not None
        row.status = "active"
    response = tenant.get("/api/licenses/current", headers=headers)
    assert response.status_code == 402, response.text
    assert "lifecycle state" in response.json()["error"].lower()

    with SessionLocal.begin() as db:
        row = db.get(License, license_id)
        assert row is not None
        row.status = "unused"
    activate(tenant, origin, workspace_id, issued["licenseKey"])
    revoked = superadmin.post(
        f"/api/superadmin/licenses/{license_id}/revoke",
        headers=origin,
        json={"reason": "tamper regression"},
    )
    assert revoked.status_code == 200, revoked.text
    with SessionLocal.begin() as db:
        row = db.get(License, license_id)
        assert row is not None
        row.status = "active"
    response = tenant.get("/api/licenses/current", headers=headers)
    assert response.status_code == 402, response.text
    assert "lifecycle state" in response.json()["error"].lower()


def test_workspace_storage_quota_rejection_removes_new_file(
    tenant: TestClient,
    superadmin: TestClient,
    origin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    workspace_id = register(tenant, origin, "quota@example.com", "Quota tenant")
    entitlement = issue(superadmin, origin, workspace_id)
    activate(tenant, origin, workspace_id, entitlement["licenseKey"])
    settings = get_settings()
    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", tmp_path / "files")
    monkeypatch.setattr(settings, "MAX_WORKSPACE_STORAGE_BYTES", 10)
    monkeypatch.setattr(settings, "MIN_STORAGE_FREE_BYTES", 0)
    headers = origin | {"x-workspace-id": str(workspace_id)}

    first = tenant.post(
        "/api/files?category=knowledge",
        headers=headers,
        files={"file": ("first.txt", b"123456", "text/plain")},
    )
    second = tenant.post(
        "/api/files?category=knowledge",
        headers=headers,
        files={"file": ("second.txt", b"abcde", "text/plain")},
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 507, second.text
    with SessionLocal() as db:
        rows = db.scalars(select(StoredFile).where(StoredFile.workspace_id == workspace_id)).all()
        assert len(rows) == 1 and rows[0].size == 6
    files = [path for path in settings.FILE_STORAGE_ROOT.rglob("*") if path.is_file()]
    assert len(files) == 1 and files[0].read_bytes() == b"123456"


def test_upload_commit_failure_rolls_back_database_and_disk(
    tenant: TestClient,
    superadmin: TestClient,
    origin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    workspace_id = register(tenant, origin, "commitfail@example.com", "Commit failure tenant")
    entitlement = issue(superadmin, origin, workspace_id)
    activate(tenant, origin, workspace_id, entitlement["licenseKey"])
    settings = get_settings()
    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", tmp_path / "files")
    monkeypatch.setattr(settings, "MIN_STORAGE_FREE_BYTES", 0)

    def fail_commit(_session: Session) -> None:
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(Session, "commit", fail_commit)
    client = TestClient(tenant.app, raise_server_exceptions=False)
    client.cookies.update(tenant.cookies)
    response = client.post(
        "/api/files?category=knowledge",
        headers=origin | {"x-workspace-id": str(workspace_id)},
        files={"file": ("orphan.txt", b"must-not-remain", "text/plain")},
    )
    assert response.status_code == 500
    assert not [path for path in settings.FILE_STORAGE_ROOT.rglob("*") if path.is_file()]
    with SessionLocal() as db:
        assert db.scalar(select(StoredFile).where(StoredFile.workspace_id == workspace_id)) is None


def test_upload_fails_closed_when_disk_reserve_cannot_be_preserved(
    tenant: TestClient,
    superadmin: TestClient,
    origin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    workspace_id = register(tenant, origin, "diskfloor@example.com", "Disk floor tenant")
    entitlement = issue(superadmin, origin, workspace_id)
    activate(tenant, origin, workspace_id, entitlement["licenseKey"])
    settings = get_settings()
    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", tmp_path / "files")
    monkeypatch.setattr(settings, "MIN_STORAGE_FREE_BYTES", 101)
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1_000, used=900, free=100),
    )
    response = tenant.post(
        "/api/files?category=knowledge",
        headers=origin | {"x-workspace-id": str(workspace_id)},
        files={"file": ("blocked.txt", b"blocked", "text/plain")},
    )
    assert response.status_code == 507
    assert not [path for path in settings.FILE_STORAGE_ROOT.rglob("*") if path.is_file()]


def test_livekit_urls_are_exact_origins_only() -> None:
    settings = get_settings()
    assert _assert_public_livekit_url("ws://VOICE.test:7880/", settings) == "ws://voice.test:7880"
    invalid_public = [
        "ws://voice.test/room",
        "ws://voice.test?token=value",
        "ws://voice.test#fragment",
        "ws://voice.test?",
        "ws://voice.test#",
        "ws://user@voice.test",
        "ws://voice.test:invalid",
        "ws://voice.test:",
        " ws://voice.test",
        "ws://voice.test\\@attacker.test",
    ]
    for value in invalid_public:
        with pytest.raises(HTTPException) as exc:
            _assert_public_livekit_url(value, settings)
        assert exc.value.status_code == 503
    with pytest.raises(HTTPException):
        _livekit_service_fields(
            "secret",
            {"internal_url": "wss://voice.test/admin", "api_key": "key"},
            "platform",
        )
    assert (
        _livekit_service_fields(
            "secret",
            {"internal_url": "wss://VOICE.test:7443/", "api_key": "key"},
            "platform",
        )[0]
        == "https://voice.test:7443"
    )


def test_completion_lock_order_is_workspace_then_license_then_call() -> None:
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
    assert "FROM workspaces" in rendered[0]
    assert "FOR UPDATE OF workspaces" in rendered[0]
    assert "FROM licenses JOIN call_sessions" in rendered[1]
    assert "FOR UPDATE OF licenses" in rendered[1]
    assert "FROM call_sessions" in rendered[2]
    assert "FOR UPDATE OF call_sessions" in rendered[2]
    assert all(statement._for_update_arg is not None for statement in statements)
