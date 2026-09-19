from __future__ import annotations

import json
import socket
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Agent, CallSession, Integration, License, PostCallResult
from app.postcall import (
    ALLOWED_QA_MODELS,
    WebhookDelivery,
    execute_post_call,
    prepare_post_call_jobs,
    snapshot_post_call_plan,
)
from app.provider_runtime import CompletionResult
from app.security import now_utc
from tests.test_control_plane import activate, issue, register


def _save_integration(
    client: TestClient,
    origin: dict[str, str],
    workspace_id: int,
    *,
    name: str = "CRM webhook",
    auth_type: str = "bearer",
    secret: str | None = "test-webhook-secret-123",  # noqa: S107
) -> dict:
    payload: dict[str, object] = {
        "name": name,
        "kind": "webhook",
        "baseUrl": "https://hooks.example.com",
        "authType": auth_type,
        "config": {"timeoutMs": "1500"},
    }
    if secret is not None:
        payload["secret"] = secret
    response = client.post(
        "/api/integrations",
        headers=origin | {"x-workspace-id": str(workspace_id)},
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()["integration"]


def _create_agent_with_post_call_nodes(
    client: TestClient,
    origin: dict[str, str],
    workspace_id: int,
    integration_id: int,
    *,
    include_qa: bool = False,
) -> dict:
    nodes: list[dict] = [{"id": "call-end", "type": "End", "label": "Call complete"}]
    edges: list[dict] = []
    previous = "call-end"
    if include_qa:
        nodes.append(
            {
                "id": "qa-after-call",
                "type": "QA",
                "label": "Quality review",
                "config": {"rubric": "Score policy compliance and resolution.", "passThreshold": 75},
            }
        )
        edges.append({"id": "end-to-qa", "source": previous, "target": "qa-after-call", "label": "Review"})
        previous = "qa-after-call"
    nodes.append(
        {
            "id": "crm-webhook",
            "type": "Webhook",
            "label": "Notify CRM",
            "config": {
                "integrationId": integration_id,
                "path": "/events/calls?source=agent",
                "method": "POST",
            },
        }
    )
    edges.append({"id": "post-to-webhook", "source": previous, "target": "crm-webhook", "label": "Notify"})
    response = client.post(
        "/api/agents",
        headers=origin | {"x-workspace-id": str(workspace_id)},
        json={
            "name": "Post-call agent",
            "objective": "Help customers",
            "workflow": {"nodes": nodes, "edges": edges},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent"]


def _seed_completed_call(
    workspace_id: int, agent_id: int, transcript: str = "Customer issue resolved"
) -> int:
    with SessionLocal.begin() as db:
        license_id = db.scalar(
            select(License.id).where(License.workspace_id == workspace_id, License.status == "active")
        )
        assert license_id is not None
        agent = db.get(Agent, agent_id)
        assert agent is not None
        call = CallSession(
            workspace_id=workspace_id,
            license_id=license_id,
            agent_id=agent_id,
            room_name=f"call-w{workspace_id}-a{agent_id}-postcalltest",
            status="completed",
            direction="test",
            transcript=transcript,
            summary="Resolved",
            duration_seconds=30,
            reserved_voice_seconds=300,
            details={"postCallPlan": snapshot_post_call_plan(db, agent)},
        )
        db.add(call)
        db.flush()
        return call.id


def test_integrations_are_secret_safe_and_cross_tenant_references_are_denied(tenant, superadmin, origin):
    first_id = register(tenant, origin, "integrations-one@example.com", "Integration tenant one")
    first_license = issue(superadmin, origin, first_id)
    activate(tenant, origin, first_id, first_license["licenseKey"])
    saved = _save_integration(
        tenant,
        origin,
        first_id,
        auth_type="bearer",
        secret="tenant-webhook-secret-123",  # noqa: S106
    )
    assert saved["hasSecret"] is True
    assert "tenant-webhook-secret-123" not in json.dumps(saved)
    assert "encryptedSecret" not in saved and "secretNonce" not in saved
    with SessionLocal() as db:
        stored = db.get(Integration, saved["id"])
        assert stored is not None
        assert "tenant-webhook-secret-123" not in stored.encrypted_secret
    reserved_header = tenant.post(
        "/api/integrations",
        headers=origin | {"x-workspace-id": str(first_id)},
        json={
            "name": "Unsafe header",
            "kind": "webhook",
            "baseUrl": "https://hooks.example.com",
            "authType": "api-key",
            "secret": "not-a-real-secret",
            "config": {"headerName": "Host"},
        },
    )
    assert reserved_header.status_code == 422

    other = TestClient(tenant.app)
    second_id = register(other, origin, "integrations-two@example.com", "Integration tenant two")
    second_license = issue(superadmin, origin, second_id)
    activate(other, origin, second_id, second_license["licenseKey"])
    listed = other.get("/api/integrations", headers={"x-workspace-id": str(second_id)})
    assert listed.status_code == 200
    assert listed.json()["integrations"] == []
    attempted = other.post(
        "/api/agents",
        headers=origin | {"x-workspace-id": str(second_id)},
        json={
            "name": "Cross-tenant reference",
            "workflow": {
                "nodes": [
                    {"id": "call-end", "type": "End", "label": "End"},
                    {
                        "id": "foreign-hook",
                        "type": "Webhook",
                        "label": "Foreign hook",
                        "config": {"integrationId": saved["id"], "path": "/event", "method": "POST"},
                    },
                ],
                "edges": [
                    {
                        "id": "end-to-foreign-hook",
                        "source": "call-end",
                        "target": "foreign-hook",
                    }
                ],
            },
        },
    )
    assert attempted.status_code == 422
    assert "unavailable tenant integration" in attempted.json()["error"]


def test_post_call_webhook_denies_private_dns_without_network(tenant, superadmin, origin, monkeypatch):
    workspace_id = register(tenant, origin, "ssrf@example.com", "SSRF tenant")
    entitlement = issue(superadmin, origin, workspace_id)
    activate(tenant, origin, workspace_id, entitlement["licenseKey"])
    integration = _save_integration(tenant, origin, workspace_id)
    agent = _create_agent_with_post_call_nodes(tenant, origin, workspace_id, integration["id"])
    call_id = _seed_completed_call(workspace_id, agent["id"])

    monkeypatch.setattr(
        "app.postcall.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))
        ],
    )

    def network_must_not_run(*_args, **_kwargs):
        raise AssertionError("private DNS result reached the network sender")

    monkeypatch.setattr("app.postcall._send_pinned_webhook", network_must_not_run)
    with SessionLocal() as db:
        stats = execute_post_call(db, call_id, get_settings())
    assert stats == {"evaluations": 0, "webhooks": 1, "failures": 1, "pending": 0}
    with SessionLocal() as db:
        result = db.scalar(select(PostCallResult).where(PostCallResult.call_id == call_id))
        assert result is not None
        assert result.status == "failed"
        assert result.error_code == "ssrf_private_address"
        assert result.result == {}


def test_post_call_qa_and_webhook_succeed_with_mocked_providers(tenant, superadmin, origin, monkeypatch):
    workspace_id = register(tenant, origin, "postcall@example.com", "Post-call tenant")
    entitlement = issue(superadmin, origin, workspace_id)
    activate(tenant, origin, workspace_id, entitlement["licenseKey"])
    integration = _save_integration(
        tenant,
        origin,
        workspace_id,
        auth_type="bearer",
        secret="post-call-secret-value",  # noqa: S106
    )
    agent = _create_agent_with_post_call_nodes(
        tenant, origin, workspace_id, integration["id"], include_qa=True
    )
    call_id = _seed_completed_call(workspace_id, agent["id"], transcript="The issue was resolved.")
    captured: dict[str, object] = {}

    def fake_completion(
        _db,
        claims,
        _settings,
        providers,
        model,
        messages,
        *,
        feature,
        allowed_models=None,
        **_kwargs,
    ):
        assert feature == "post_call"
        assert claims.provider_mode == "byok"
        assert providers == ["openai"]
        assert model == "gpt-4.1-mini"
        assert allowed_models == ALLOWED_QA_MODELS
        assert _kwargs.get("json_object") is True
        assert _kwargs.get("max_tokens") == 2_048
        assert "untrusted data" in messages[0]["content"]
        return CompletionResult(
            text=json.dumps(
                {
                    "score": 92,
                    "summary": "Issue for alice@example.com and +1 415 555 0123 was resolved.",
                    "sentiment": "positive",
                    "disposition": "resolved",
                }
            ),
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=90,
            output_tokens=20,
        )

    def fake_sender(method, target, host, resolved_ip, headers, body, timeout_seconds):
        captured["deliveries"] = int(captured.get("deliveries", 0)) + 1
        captured.update(
            method=method,
            target=target,
            host=host,
            resolved_ip=resolved_ip,
            headers=headers,
            payload=json.loads(body),
            timeout_seconds=timeout_seconds,
        )
        return WebhookDelivery(202, 2, "a" * 64)

    monkeypatch.setattr("app.postcall.create_chat_completion", fake_completion)
    monkeypatch.setattr("app.postcall._resolve_public_ips", lambda _host: ("93.184.216.34",))
    monkeypatch.setattr("app.postcall._send_pinned_webhook", fake_sender)
    with SessionLocal() as db:
        stats = execute_post_call(db, call_id, get_settings())
        repeated = execute_post_call(db, call_id, get_settings())
    assert stats == {"evaluations": 1, "webhooks": 1, "failures": 0, "pending": 0}
    assert repeated == stats
    assert captured["deliveries"] == 1
    assert captured["method"] == "POST"
    assert captured["target"] == "https://hooks.example.com/events/calls?source=agent"
    assert captured["headers"]["Authorization"] == "Bearer post-call-secret-value"
    assert captured["headers"]["Idempotency-Key"] == f"call-{call_id}-node-crm-webhook"
    assert captured["headers"]["X-Nexora-Signature"].startswith("sha256=")
    assert captured["headers"]["X-Nexora-Timestamp"].isdigit()
    assert captured["payload"]["qa"][0]["score"] == 92

    with SessionLocal() as db:
        rows = db.scalars(
            select(PostCallResult).where(PostCallResult.call_id == call_id).order_by(PostCallResult.id)
        ).all()
        call = db.get(CallSession, call_id)
        assert [row.status for row in rows] == ["succeeded", "succeeded"]
        assert all("post-call-secret-value" not in json.dumps(row.result) for row in rows)
        assert rows[0].result["summary"].count("[REDACTED]") == 2
        assert call is not None
        assert call.pipeline_completed is True
        assert call.sentiment == "positive"
        assert call.disposition == "RESOLVED"
        assert call.details["postCall"]["failures"] == 0


def test_inflight_call_rejects_changed_integration_origin(tenant, superadmin, origin, monkeypatch):
    workspace_id = register(tenant, origin, "frozen-origin@example.com", "Frozen origin tenant")
    entitlement = issue(superadmin, origin, workspace_id)
    activate(tenant, origin, workspace_id, entitlement["licenseKey"])
    integration = _save_integration(tenant, origin, workspace_id)
    agent = _create_agent_with_post_call_nodes(tenant, origin, workspace_id, integration["id"])
    call_id = _seed_completed_call(workspace_id, agent["id"])
    with SessionLocal.begin() as db:
        row = db.get(Integration, integration["id"])
        assert row is not None
        row.base_url = "https://changed.example.com"

    def network_must_not_run(*_args, **_kwargs):
        raise AssertionError("changed integration origin reached the network")

    monkeypatch.setattr("app.postcall._send_pinned_webhook", network_must_not_run)
    with SessionLocal() as db:
        stats = execute_post_call(db, call_id, get_settings())
        result = db.scalar(select(PostCallResult).where(PostCallResult.call_id == call_id))
        assert result is not None
        assert result.error_code == "integration_origin_changed"
    assert stats["failures"] == 1


def test_running_outbox_job_is_not_claimed_twice(tenant, superadmin, origin, monkeypatch):
    workspace_id = register(tenant, origin, "outbox-lock@example.com", "Outbox lock tenant")
    entitlement = issue(superadmin, origin, workspace_id)
    activate(tenant, origin, workspace_id, entitlement["licenseKey"])
    integration = _save_integration(tenant, origin, workspace_id)
    agent = _create_agent_with_post_call_nodes(tenant, origin, workspace_id, integration["id"])
    call_id = _seed_completed_call(workspace_id, agent["id"])
    with SessionLocal.begin() as db:
        call = db.get(CallSession, call_id)
        assert call is not None
        prepare_post_call_jobs(db, call)
        result = db.scalar(select(PostCallResult).where(PostCallResult.call_id == call_id))
        assert result is not None
        result.status = "running"
        result.attempts = 1
        result.lease_expires_at = now_utc() + timedelta(minutes=4)

    def network_must_not_run(*_args, **_kwargs):
        raise AssertionError("an actively leased job was claimed twice")

    monkeypatch.setattr("app.postcall._send_pinned_webhook", network_must_not_run)
    with SessionLocal() as db:
        stats = execute_post_call(db, call_id, get_settings())
    assert stats == {"evaluations": 0, "webhooks": 1, "failures": 0, "pending": 1}
