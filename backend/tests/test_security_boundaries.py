from __future__ import annotations

import base64
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.licensing import PROVIDER_KINDS, active_license_for_workspace
from app.models import Agent, Membership, ProviderConnection, User, WorkspaceInvite
from app.provider_vault import resolve_runtime_credential


def _register(client: TestClient, origin: dict[str, str], email: str, workspace: str) -> int:
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


def _issue_and_activate(
    tenant: TestClient,
    superadmin: TestClient,
    origin: dict[str, str],
    workspace_id: int,
    *,
    features: list[str],
    provider_mode: str = "byok",
    hybrid_policy: dict[str, str] | None = None,
    seats: int = 6,
) -> None:
    now = datetime.now(UTC)
    issued = superadmin.post(
        "/api/superadmin/licenses",
        headers=origin,
        json={
            "workspaceId": workspace_id,
            "plan": "boundary-tests",
            "seats": seats,
            "validFrom": (now - timedelta(minutes=1)).isoformat(),
            "validUntil": (now + timedelta(days=1)).isoformat(),
            "providerMode": provider_mode,
            "hybridPolicy": hybrid_policy or {},
            "features": features,
        },
    )
    assert issued.status_code == 201, issued.text
    activated = tenant.post(
        "/api/licenses/activate",
        headers=origin | {"x-workspace-id": str(workspace_id)},
        json={"licenseKey": issued.json()["licenseKey"]},
    )
    assert activated.status_code == 200, activated.text


def _invite(
    owner: TestClient,
    origin: dict[str, str],
    workspace_id: int,
    email: str,
    role: str = "member",
) -> tuple[int, str]:
    response = owner.post(
        "/api/invites",
        headers=origin | {"x-workspace-id": str(workspace_id)},
        json={"email": email, "role": role},
    )
    assert response.status_code == 201, response.text
    return response.json()["invite"]["id"], response.json()["inviteToken"]


def _register_invited(
    app_client: TestClient,
    origin: dict[str, str],
    email: str,
    invite_token: str,
) -> tuple[TestClient, int]:
    member = TestClient(app_client.app)
    response = member.post(
        "/api/auth/register",
        headers=origin,
        json={
            "email": email,
            "password": "Member-password-123!",
            "name": "Member",
            "inviteToken": invite_token,
        },
    )
    assert response.status_code == 201, response.text
    return member, response.json()["user"]["id"]


def test_provider_routes_separate_telephony_reject_public_secrets_and_fail_closed(tenant, superadmin, origin):
    workspace_id = _register(tenant, origin, "provider-boundary@example.com", "Provider boundary")
    policy = {kind: "platform" for kind in PROVIDER_KINDS}
    policy["llm"] = "byok"
    _issue_and_activate(
        tenant,
        superadmin,
        origin,
        workspace_id,
        features=["providers", "telephony"],
        provider_mode="hybrid",
        hybrid_policy=policy,
    )
    headers = origin | {"x-workspace-id": str(workspace_id)}

    telephony = tenant.post(
        "/api/providers",
        headers=headers,
        json={"kind": "telephony", "provider": "twilio", "secret": "tenant-value"},
    )
    assert telephony.status_code == 422
    assert "telephony endpoint" in telephony.json()["error"]
    for config in ({"baseUrl": "https://attacker.example"}, {"apiKey": "plaintext"}):
        unsafe_config = tenant.post(
            "/api/providers",
            headers=headers,
            json={
                "kind": "llm",
                "provider": "openai",
                "secret": "tenant-value",
                "config": config,
            },
        )
        assert unsafe_config.status_code == 422

    with SessionLocal() as db:
        claims = active_license_for_workspace(db, workspace_id, get_settings())
        with pytest.raises(Exception) as missing:
            resolve_runtime_credential(db, claims, "llm", "openai", get_settings())
        assert getattr(missing.value, "status_code", None) == 503
        assert (
            db.scalar(select(ProviderConnection).where(ProviderConnection.workspace_id == workspace_id))
            is None
        )


def test_member_role_remove_and_invite_revoke_are_tenant_scoped(tenant, superadmin, origin):
    workspace_id = _register(tenant, origin, "owner-boundary@example.com", "Owner boundary")
    _issue_and_activate(tenant, superadmin, origin, workspace_id, features=["members"])
    headers = origin | {"x-workspace-id": str(workspace_id)}

    _member_invite_id, member_token = _invite(tenant, origin, workspace_id, "member-boundary@example.com")
    member, member_id = _register_invited(tenant, origin, "member-boundary@example.com", member_token)
    promoted = tenant.patch(f"/api/members/{member_id}", headers=headers, json={"role": "operator"})
    assert promoted.status_code == 200, promoted.text

    with SessionLocal() as db:
        owner_id = db.scalar(select(User.id).where(User.email == "owner-boundary@example.com"))
        assert owner_id is not None
    assert tenant.delete(f"/api/members/{owner_id}", headers=headers).status_code == 403
    assert member.delete(f"/api/members/{owner_id}", headers=headers).status_code == 403

    invite_id, revoked_token = _invite(tenant, origin, workspace_id, "revoked@example.com")
    revoked = tenant.delete(f"/api/invites/{invite_id}", headers=headers)
    assert revoked.status_code == 200, revoked.text
    rejected = TestClient(tenant.app).post(
        "/api/auth/register",
        headers=origin,
        json={
            "email": "revoked@example.com",
            "password": "Member-password-123!",
            "name": "Rejected",
            "inviteToken": revoked_token,
        },
    )
    assert rejected.status_code == 400

    other = TestClient(tenant.app)
    other_workspace = _register(other, origin, "other-owner-boundary@example.com", "Other boundary")
    _issue_and_activate(other, superadmin, origin, other_workspace, features=["members"])
    other_headers = origin | {"x-workspace-id": str(other_workspace)}
    assert other.delete(f"/api/members/{member_id}", headers=other_headers).status_code == 404
    assert other.delete(f"/api/invites/{invite_id}", headers=other_headers).status_code == 404

    removed = tenant.delete(f"/api/members/{member_id}", headers=headers)
    assert removed.status_code == 200, removed.text
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(Membership).where(
                    Membership.workspace_id == workspace_id,
                    Membership.user_id == member_id,
                )
            )
            is None
        )
        invite = db.get(WorkspaceInvite, invite_id)
        assert invite is not None and invite.revoked_at is not None


def _agent_payload(case: str) -> dict[str, object]:
    credential_marker = "sk-" + "a" * 24
    payload: dict[str, object] = {
        "name": "Secret boundary",
        "objective": "Help customers",
        "workflow": {"nodes": [], "edges": []},
    }
    if case == "global_prompt":
        payload["globalPrompt"] = f"Use API key: {credential_marker}"
    elif case == "node_prompt":
        payload["workflow"] = {
            "nodes": [
                {
                    "id": "agent-1",
                    "type": "Agent",
                    "label": "Agent",
                    "prompt": f"access_token={credential_marker}",
                }
            ],
            "edges": [],
        }
    elif case == "model_identifier":
        payload["model"] = credential_marker
    elif case == "node_identifier":
        payload["workflow"] = {
            "nodes": [{"id": credential_marker, "type": "Agent", "label": "Agent"}],
            "edges": [],
        }
    return deepcopy(payload)


@pytest.mark.parametrize(
    "case",
    ["global_prompt", "node_prompt", "model_identifier", "node_identifier"],
)
def test_agent_definition_rejects_embedded_secrets(case, tenant, superadmin, origin):
    workspace_id = _register(tenant, origin, f"agent-{case}@example.com", f"Agent {case}")
    _issue_and_activate(tenant, superadmin, origin, workspace_id, features=["agents"])
    response = tenant.post(
        "/api/agents",
        headers=origin | {"x-workspace-id": str(workspace_id)},
        json=_agent_payload(case),
    )
    assert response.status_code == 422, response.text
    assert "credential" in response.json()["error"].lower()
    with SessionLocal() as db:
        assert db.scalar(select(Agent).where(Agent.workspace_id == workspace_id)) is None


def _raw_private_key(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def _raw_public_key(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def test_production_settings_reject_mismatched_license_signing_keys(tmp_path):
    private = Ed25519PrivateKey.generate()
    unrelated = Ed25519PrivateKey.generate()
    key32 = base64.b64encode(os.urandom(32)).decode()

    with pytest.raises(ValidationError, match="public/private keys do not match"):
        Settings(
            _env_file=None,
            ENVIRONMENT="production",
            DATABASE_URL="postgresql+psycopg://nexora:password@db/nexora",
            PUBLIC_BASE_URL="https://app.example.com",
            ALLOWED_ORIGINS="https://app.example.com",
            SESSION_COOKIE_NAME="__Host-nexora_session",
            SESSION_COOKIE_SECURE=True,
            FILE_STORAGE_ROOT=tmp_path,
            CREDENTIAL_MASTER_KEY=key32,
            PHONE_HASH_KEY=key32,
            WORKER_CONFIG_KEY=key32,
            CALL_WORKER_TOKEN="worker-token-" + "x" * 40,
            LICENSE_SIGNING_PRIVATE_KEY=base64.b64encode(_raw_private_key(private)).decode(),
            LICENSE_SIGNING_PUBLIC_KEY=base64.b64encode(_raw_public_key(unrelated)).decode(),
        )
