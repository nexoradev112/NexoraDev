from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.licensing import PROVIDER_KINDS, active_license_for_workspace
from app.models import ProviderConnection
from app.provider_vault import resolve_runtime_credential


def _register(client: TestClient, origin: dict[str, str], email: str, name: str) -> int:
    response = client.post(
        "/api/auth/register",
        headers=origin,
        json={
            "email": email,
            "password": "Tenant-password-123!",
            "name": "Owner",
            "workspaceName": name,
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
) -> None:
    now = datetime.now(UTC)
    response = superadmin.post(
        "/api/superadmin/licenses",
        headers=origin,
        json={
            "workspaceId": workspace_id,
            "plan": "provider-tests",
            "seats": 3,
            "validFrom": (now - timedelta(minutes=1)).isoformat(),
            "validUntil": (now + timedelta(days=1)).isoformat(),
            "providerMode": provider_mode,
            "hybridPolicy": hybrid_policy or {},
            "features": features,
        },
    )
    assert response.status_code == 201, response.text
    activation = tenant.post(
        "/api/licenses/activate",
        headers=origin | {"x-workspace-id": str(workspace_id)},
        json={"licenseKey": response.json()["licenseKey"]},
    )
    assert activation.status_code == 200, activation.text


def test_generic_provider_route_rejects_telephony(tenant, superadmin, origin):
    workspace_id = _register(tenant, origin, "provider-owner@example.com", "Provider tenant")
    _issue_and_activate(
        tenant,
        superadmin,
        origin,
        workspace_id,
        features=["providers", "telephony"],
    )
    headers = origin | {"x-workspace-id": str(workspace_id)}

    response = tenant.post(
        "/api/providers",
        headers=headers,
        json={
            "kind": "telephony",
            "provider": "twilio",
            "secret": "not-a-real-credential",
        },
    )

    assert response.status_code == 422
    assert "telephony endpoint" in response.json()["error"]
    with SessionLocal() as db:
        assert (
            db.scalar(select(ProviderConnection).where(ProviderConnection.workspace_id == workspace_id))
            is None
        )


def test_generic_public_config_is_capability_scoped_and_legacy_values_are_filtered(
    tenant, superadmin, origin
):
    workspace_id = _register(tenant, origin, "config-owner@example.com", "Config tenant")
    _issue_and_activate(tenant, superadmin, origin, workspace_id, features=["providers"])
    headers = origin | {"x-workspace-id": str(workspace_id)}

    for config in (
        {"baseUrl": "https://attacker.example"},
        {"apiKey": "plaintext-secret"},
        {"voiceId": "wrong-capability"},
        {"model": "gpt-4o-mini\nX-Injected: true"},
    ):
        response = tenant.post(
            "/api/providers",
            headers=headers,
            json={
                "kind": "llm",
                "provider": "openai",
                "secret": "tenant-openai-key",
                "config": config,
            },
        )
        assert response.status_code == 422, response.text

    groq = tenant.post(
        "/api/providers",
        headers=headers,
        json={
            "kind": "llm",
            "provider": "groq",
            "secret": "tenant-groq-key",
            "config": {"model": "openai/gpt-oss-120b"},
        },
    )
    assert groq.status_code == 201, groq.text
    assert groq.json()["connection"]["config"] == {"model": "openai/gpt-oss-120b"}

    # Defense in depth for a database imported from the earlier implementation:
    # public serializers must not echo formerly accepted unsafe fields.
    with SessionLocal.begin() as db:
        row = db.scalar(
            select(ProviderConnection).where(
                ProviderConnection.workspace_id == workspace_id,
                ProviderConnection.kind == "llm",
            )
        )
        assert row is not None
        row.config = {
            "model": "gpt-4.1-mini",
            "baseUrl": "https://attacker.example",
            "apiKey": "plaintext-secret",
        }

    listed = tenant.get("/api/providers", headers={"x-workspace-id": str(workspace_id)})
    assert listed.status_code == 200, listed.text
    assert listed.json()["connections"][0]["config"] == {"model": "gpt-4.1-mini"}
    assert "telephony" not in listed.json()["supported"]
    assert "deepgram" in listed.json()["supported"]["tts"]
    assert "deepgram" in listed.json()["supported"]["stt"]


def test_deepgram_tts_accepts_aura_model_and_voice(tenant, superadmin, origin):
    workspace_id = _register(tenant, origin, "deepgram-tts@example.com", "Deepgram TTS tenant")
    _issue_and_activate(tenant, superadmin, origin, workspace_id, features=["providers"])
    headers = origin | {"x-workspace-id": str(workspace_id)}

    saved = tenant.post(
        "/api/providers",
        headers=headers,
        json={
            "kind": "tts",
            "provider": "deepgram",
            "secret": "tenant-deepgram-tts-key",
            "config": {"model": "aura-2-andromeda-en", "voice": "thalia"},
        },
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["connection"]["provider"] == "deepgram"
    assert saved.json()["connection"]["kind"] == "tts"
    assert saved.json()["connection"]["config"] == {"model": "aura-2-andromeda-en", "voice": "thalia"}


def test_disable_uses_feature_for_stored_kind_and_stays_tenant_scoped(tenant, superadmin, origin):
    workspace_id = _register(tenant, origin, "phone-owner@example.com", "Phone tenant")
    _issue_and_activate(tenant, superadmin, origin, workspace_id, features=["telephony"])
    headers = origin | {"x-workspace-id": str(workspace_id)}
    saved = tenant.post(
        "/api/telephony",
        headers=headers,
        json={
            "provider": "twilio",
            "credentials": {"accountSid": "AC123", "authToken": "tenant-auth-token"},
            "config": {"callerIds": "+12025550123"},
        },
    )
    assert saved.status_code == 201, saved.text
    connection_id = saved.json()["connection"]["id"]

    with SessionLocal.begin() as db:
        row = db.get(ProviderConnection, connection_id)
        assert row is not None
        row.config = {"callerIds": "+12025550123", "apiKey": "legacy-plaintext-secret"}
    listed = tenant.get("/api/telephony", headers={"x-workspace-id": str(workspace_id)})
    assert listed.status_code == 200, listed.text
    assert listed.json()["connections"][0]["config"] == {"callerIds": "+12025550123"}

    disabled = tenant.delete(f"/api/providers/{connection_id}", headers=headers)
    assert disabled.status_code == 200, disabled.text
    with SessionLocal() as db:
        row = db.get(ProviderConnection, connection_id)
        assert row is not None and row.status == "disabled"

    other = TestClient(tenant.app)
    other_workspace = _register(other, origin, "other-provider@example.com", "Other provider tenant")
    _issue_and_activate(other, superadmin, origin, other_workspace, features=["providers"])
    other_headers = origin | {"x-workspace-id": str(other_workspace)}
    cross_tenant = other.delete(f"/api/providers/{connection_id}", headers=other_headers)
    assert cross_tenant.status_code == 404

    legacy_ciphertext = f"legacy-ciphertext-{other_workspace}"
    legacy_nonce = f"legacy-nonce-{other_workspace}"
    with SessionLocal.begin() as db:
        legacy = ProviderConnection(
            workspace_id=other_workspace,
            kind="telephony",
            provider="twilio",
            label="Legacy phone",
            encrypted_secret=legacy_ciphertext,
            secret_nonce=legacy_nonce,
            key_version=1,
            config={},
            status="active",
        )
        db.add(legacy)
        db.flush()
        legacy_id = legacy.id

    denied = other.delete(f"/api/providers/{legacy_id}", headers=other_headers)
    assert denied.status_code == 403
    with SessionLocal() as db:
        row = db.get(ProviderConnection, legacy_id)
        assert row is not None and row.status == "active"


def test_hybrid_byok_kind_never_uses_platform_key(tenant, superadmin, origin):
    workspace_id = _register(tenant, origin, "hybrid-owner@example.com", "Hybrid tenant")
    policy = {kind: "platform" for kind in PROVIDER_KINDS}
    policy["llm"] = "byok"
    _issue_and_activate(
        tenant,
        superadmin,
        origin,
        workspace_id,
        features=["providers"],
        provider_mode="hybrid",
        hybrid_policy=policy,
    )

    with SessionLocal() as db:
        claims = active_license_for_workspace(db, workspace_id, get_settings())
        try:
            resolve_runtime_credential(db, claims, "llm", "openai", get_settings())
        except Exception as exc:  # FastAPI raises HTTPException at the service boundary.
            assert getattr(exc, "status_code", None) == 503
            assert "Tenant openai credential" in getattr(exc, "detail", "")
        else:
            raise AssertionError("hybrid BYOK LLM unexpectedly fell back to the platform key")
