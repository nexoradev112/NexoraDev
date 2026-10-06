from __future__ import annotations

import base64
import json
import secrets
import time
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.licensing import active_license_for_workspace
from app.models import License, Membership, StoredFile, User
from app.provider_vault import resolve_runtime_credential
from app.security import hash_password, worker_signature
from app.storage import category_directory
from tests.conftest import WORKER_KEY


def register(client: TestClient, origin: dict[str, str], email: str, workspace: str) -> int:
    response = client.post(
        "/api/auth/register",
        headers=origin,
        json={
            "email": email,
            "password": "Tenant-password-123!",
            "name": "Owner",
            "workspaceName": workspace,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["workspaceId"]


def issue(
    superadmin: TestClient,
    origin: dict[str, str],
    workspace_id: int,
    mode: str = "byok",
) -> dict:
    now = datetime.now(UTC)
    features = [
        "agents",
        "analytics",
        "chat",
        "members",
        "post_call",
        "providers",
        "recordings",
        "telephony",
    ]
    if mode == "platform":
        features.append("voice")
    response = superadmin.post(
        "/api/superadmin/licenses",
        headers=origin,
        json={
            "workspaceId": workspace_id,
            "plan": "pro",
            "seats": 3,
            "validFrom": (now - timedelta(minutes=1)).isoformat(),
            "validUntil": (now + timedelta(days=30)).isoformat(),
            "providerMode": mode,
            "quotas": {"agents": 5, "tokens": 50_000, "voice_seconds": 5_000},
            "features": features,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def activate(client: TestClient, origin: dict[str, str], workspace_id: int, key: str) -> None:
    response = client.post(
        "/api/licenses/activate",
        headers=origin | {"x-workspace-id": str(workspace_id)},
        json={"licenseKey": key},
    )
    assert response.status_code == 200, response.text


def signed_worker_post(client: TestClient, path: str, payload: dict, *, nonce: str | None = None):
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = int(time.time())
    nonce = nonce or secrets.token_urlsafe(24)
    token = get_settings().CALL_WORKER_TOKEN
    signature = worker_signature(token, timestamp, nonce, "POST", path, body)
    return client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Worker-Timestamp": str(timestamp),
            "X-Worker-Nonce": nonce,
            "X-Worker-Signature": signature,
        },
    )


def test_license_is_one_time_and_tenant_crud_is_isolated(tenant, superadmin, origin):
    first_id = register(tenant, origin, "owner@example.com", "First tenant")
    entitlement = issue(superadmin, origin, first_id)
    key = entitlement["licenseKey"]
    activate(tenant, origin, first_id, key)
    headers = origin | {"x-workspace-id": str(first_id)}
    saved = tenant.post(
        "/api/providers",
        headers=headers,
        json={"kind": "llm", "provider": "openai", "secret": "tenant-fake-key"},
    )
    assert saved.status_code == 201
    agent = tenant.post(
        "/api/agents",
        headers=headers,
        json={"name": "Support", "objective": "Help customers", "workflow": {"nodes": [], "edges": []}},
    )
    assert agent.status_code == 201

    second = TestClient(tenant.app)
    second_id = register(second, origin, "other@example.com", "Other tenant")
    assert tenant.get("/api/agents", headers={"x-workspace-id": str(second_id)}).status_code == 404

    with SessionLocal() as db:
        row = db.scalar(select(License).where(License.workspace_id == first_id))
        assert row is not None
        reconstructed_without_activation_secret = f"nxlic_v1.fake.{row.signature}"
        assert (
            row.token_hash
            != __import__("hashlib").sha256(reconstructed_without_activation_secret.encode()).hexdigest()
        )


def test_byok_never_falls_back_to_platform_key(tenant, superadmin, origin):
    workspace_id = register(tenant, origin, "byok@example.com", "BYOK tenant")
    entitlement = issue(superadmin, origin, workspace_id, "byok")
    activate(tenant, origin, workspace_id, entitlement["licenseKey"])
    with SessionLocal() as db:
        claims = active_license_for_workspace(db, workspace_id, get_settings())
        try:
            resolve_runtime_credential(db, claims, "llm", "openai", get_settings())
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 503
        else:
            raise AssertionError("BYOK unexpectedly used the configured platform key")


def test_origin_required_and_worker_envelope_is_short_lived(tenant, superadmin, origin):
    assert (
        tenant.post(
            "/api/auth/login", json={"email": "none@example.com", "password": "irrelevant"}
        ).status_code
        == 403
    )
    workspace_id = register(tenant, origin, "voice@example.com", "Voice tenant")
    entitlement = issue(superadmin, origin, workspace_id, "platform")
    activate(tenant, origin, workspace_id, entitlement["licenseKey"])
    headers = origin | {"x-workspace-id": str(workspace_id)}
    agent = tenant.post(
        "/api/agents",
        headers=headers,
        json={
            "name": "Voice",
            "objective": "Help",
            "maxCallSeconds": 300,
            "workflow": {"nodes": [], "edges": []},
        },
    ).json()["agent"]
    token = tenant.post(
        "/api/livekit/token", headers=headers, json={"agentId": agent["id"], "sessionId": "testsession"}
    )
    assert token.status_code == 200, token.text
    room = token.json()["room_name"]
    config = signed_worker_post(tenant, "/api/internal/agents/config", {"roomName": room})
    assert config.status_code == 200, config.text
    envelope = config.json()["runtimeProvidersEnvelope"]

    def decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    plaintext = AESGCM(WORKER_KEY).decrypt(
        decode(envelope["nonce"]),
        decode(envelope["ciphertext"]),
        f"nexora-runtime-providers:v1:{room}".encode(),
    )
    payload = json.loads(plaintext)
    assert payload["roomName"] == room
    assert payload["expiresAt"] - payload["issuedAt"] <= 120
    runtime = payload["runtimeProviders"]
    assert runtime["transport"] == "direct"
    assert runtime["mode"] == "platform"
    assert set(runtime) == {"transport", "mode", "hybridPolicy", "llm", "stt", "tts"}
    assert all(runtime[kind]["credentialSource"] == "platform" for kind in ("llm", "stt", "tts"))
    assert "devsecret-with-32-characters-0000" not in plaintext.decode()

    first = signed_worker_post(
        tenant,
        "/api/internal/calls/complete",
        {"roomName": room, "durationSeconds": 10, "transcript": "done"},
    )
    second = signed_worker_post(
        tenant,
        "/api/internal/calls/complete",
        {"roomName": room, "durationSeconds": 10},
    )
    assert first.status_code == second.status_code == 200
    assert second.json()["idempotent"] is True


def test_internal_request_signature_rejects_replay(tenant):
    path = "/api/internal/agents/config"
    payload = {"roomName": "test-w1-a1-replaytest"}
    nonce = "replay_nonce_1234567890"
    first = signed_worker_post(tenant, path, payload, nonce=nonce)
    second = signed_worker_post(tenant, path, payload, nonce=nonce)
    # The first request passes HMAC authentication (the unlicensed fixture then
    # fails closed). The duplicate nonce is rejected before routing.
    assert first.status_code != 401
    assert second.status_code == 401
    assert second.json()["error"] == "Worker request replay rejected"


def test_activation_rejects_existing_members_over_signed_seats(tenant, superadmin, origin):
    workspace_id = register(tenant, origin, "crowded@example.com", "Crowded tenant")
    with SessionLocal.begin() as db:
        for number in (1, 2):
            user = User(
                email=f"member{number}@example.com",
                name="Member",
                password_hash=hash_password("Member-password-123!"),
            )
            db.add(user)
            db.flush()
            db.add(Membership(workspace_id=workspace_id, user_id=user.id, role="member"))
    now = datetime.now(UTC)
    response = superadmin.post(
        "/api/superadmin/licenses",
        headers=origin,
        json={
            "workspaceId": workspace_id,
            "plan": "small",
            "seats": 1,
            "validFrom": (now - timedelta(minutes=1)).isoformat(),
            "validUntil": (now + timedelta(days=1)).isoformat(),
            "providerMode": "byok",
            "features": ["agents"],
        },
    )
    key = response.json()["licenseKey"]
    activation = tenant.post(
        "/api/licenses/activate",
        headers=origin | {"x-workspace-id": str(workspace_id)},
        json={"licenseKey": key},
    )
    assert activation.status_code == 403
    assert "seat" in activation.json()["error"].lower()


def test_recording_feature_cannot_be_bypassed_and_symlink_namespace_is_rejected(tenant, superadmin, origin):
    workspace_id = register(tenant, origin, "limited@example.com", "Limited tenant")
    now = datetime.now(UTC)
    issued = superadmin.post(
        "/api/superadmin/licenses",
        headers=origin,
        json={
            "workspaceId": workspace_id,
            "plan": "limited",
            "seats": 1,
            "validFrom": (now - timedelta(minutes=1)).isoformat(),
            "validUntil": (now + timedelta(days=1)).isoformat(),
            "providerMode": "byok",
            "features": ["agents"],
        },
    ).json()
    activate(tenant, origin, workspace_id, issued["licenseKey"])
    root = get_settings().FILE_STORAGE_ROOT
    target = root / "recordings" / f"w{workspace_id}"
    target.mkdir(parents=True, exist_ok=True)
    content = target / "test.wav"
    content.write_bytes(b"RIFF-not-real-audio")
    with SessionLocal.begin() as db:
        record = StoredFile(
            workspace_id=workspace_id,
            category="recordings",
            storage_key=f"recordings/w{workspace_id}/test.wav",
            filename="test.wav",
            content_type="audio/wav",
            size=content.stat().st_size,
            sha256="0" * 64,
        )
        db.add(record)
        db.flush()
        record_id = record.id
    response = tenant.get(f"/api/files/{record_id}/content", headers={"x-workspace-id": str(workspace_id)})
    assert response.status_code == 403

    symlink_workspace = workspace_id + 999
    link = root / "recordings" / f"w{symlink_workspace}"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(Exception) as exc:
        category_directory(get_settings(), "recordings", symlink_workspace)
    assert getattr(exc.value, "status_code", None) == 500


def test_superadmin_workspace_directory_lists_names(tenant, superadmin, origin):
    workspace_id = register(tenant, origin, "picker@example.com", "Picker Workspace")
    denied = tenant.get("/api/superadmin/workspaces", headers=origin)
    assert denied.status_code == 403
    listed = superadmin.get("/api/superadmin/workspaces", headers=origin)
    assert listed.status_code == 200, listed.text
    match = next(row for row in listed.json()["workspaces"] if row["id"] == workspace_id)
    assert match["name"] == "Picker Workspace"
    assert match["status"] == "pending_license"
