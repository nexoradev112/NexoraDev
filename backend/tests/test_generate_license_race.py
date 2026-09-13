from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Agent
from app.provider_runtime import CompletionResult


def test_generate_cannot_persist_after_concurrent_revoke(tenant, superadmin, origin, monkeypatch):
    registered = tenant.post(
        "/api/auth/register",
        headers=origin,
        json={
            "email": "generate-race@example.com",
            "password": "Tenant-password-123!",
            "name": "Owner",
            "workspaceName": "Generate race",
        },
    )
    assert registered.status_code == 201, registered.text
    workspace_id = registered.json()["workspaceId"]
    now = datetime.now(UTC)
    issued = superadmin.post(
        "/api/superadmin/licenses",
        headers=origin,
        json={
            "workspaceId": workspace_id,
            "plan": "platform",
            "seats": 1,
            "validFrom": (now - timedelta(minutes=1)).isoformat(),
            "validUntil": (now + timedelta(days=1)).isoformat(),
            "providerMode": "platform",
            "quotas": {"agents": 1, "tokens": 10000},
            "features": ["agents"],
        },
    )
    assert issued.status_code == 201, issued.text
    license_id = issued.json()["license"]["id"]
    headers = origin | {"x-workspace-id": str(workspace_id)}
    assert (
        tenant.post(
            "/api/licenses/activate",
            headers=headers,
            json={"licenseKey": issued.json()["licenseKey"]},
        ).status_code
        == 200
    )

    paid_request_started = threading.Event()
    release_provider = threading.Event()

    def fake_completion(db, *_args, **_kwargs):
        # Mirrors the real platform-funded provider path: reservation commits
        # before the network request, releasing the Workspace lock.
        db.commit()
        paid_request_started.set()
        assert release_provider.wait(5)
        value = {
            "name": "Created after revoke",
            "objective": "Safe objective",
            "greeting": "Hello",
            "globalPrompt": "Help the caller",
            "nodes": [],
            "edges": [],
        }
        return CompletionResult(json.dumps(value), "openai", "gpt-4.1-mini", 10, 10)

    monkeypatch.setattr("app.api.chat.create_chat_completion", fake_completion)
    result = {}

    def generate():
        result["response"] = tenant.post(
            "/api/agents/generate",
            headers=headers,
            json={
                "useCase": "support",
                "description": "Handle ordinary customer support questions safely.",
            },
        )

    thread = threading.Thread(target=generate)
    thread.start()
    assert paid_request_started.wait(5)
    revoked = superadmin.post(
        f"/api/superadmin/licenses/{license_id}/revoke",
        headers=origin,
        json={"reason": "race proof"},
    )
    assert revoked.status_code == 200, revoked.text
    release_provider.set()
    thread.join(5)
    assert not thread.is_alive()
    assert result["response"].status_code == 402, result["response"].text
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Agent.id)).where(Agent.workspace_id == workspace_id)) == 0
