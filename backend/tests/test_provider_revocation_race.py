from __future__ import annotations

import threading

import app.provider_runtime as provider_runtime
from app.config import get_settings
from tests.test_control_plane import activate, issue, register


def test_provider_fallback_cannot_start_after_concurrent_revoke(
    tenant,
    superadmin,
    origin,
    monkeypatch,
) -> None:
    workspace_id = register(tenant, origin, "fallback-race@example.com", "Fallback race")
    entitlement = issue(superadmin, origin, workspace_id, "platform")
    activate(tenant, origin, workspace_id, entitlement["licenseKey"])
    license_id = int(entitlement["license"]["id"])
    headers = origin | {"x-workspace-id": str(workspace_id)}
    agent_response = tenant.post(
        "/api/agents",
        headers=headers,
        json={
            "name": "Fallback race",
            "channel": "chat",
            "providerPolicy": {
                "llm": ["openai", "groq"],
                "stt": ["deepgram"],
                "tts": ["elevenlabs"],
                "realtime": ["livekit"],
            },
            "workflow": {"nodes": [], "edges": []},
        },
    )
    assert agent_response.status_code == 201, agent_response.text
    agent_id = int(agent_response.json()["agent"]["id"])
    monkeypatch.setattr(get_settings(), "GROQ_API_KEY", "platform-groq-test-key")

    first_attempt_started = threading.Event()
    release_first_attempt = threading.Event()
    groq_called = threading.Event()

    def fake_openai_compatible(credential, model, messages, **_kwargs):
        del model, messages
        if credential.provider == "openai":
            first_attempt_started.set()
            assert release_first_attempt.wait(5)
            raise provider_runtime.ProviderResponseError(
                "first provider failed",
                cost_uncertain=False,
            )
        groq_called.set()
        raise AssertionError("Fallback provider ran after revocation")

    monkeypatch.setattr(provider_runtime, "_openai_compatible", fake_openai_compatible)
    result: dict[str, object] = {}

    def run_chat() -> None:
        result["response"] = tenant.post(
            "/api/chat",
            headers=headers,
            json={"agentId": agent_id, "messages": [{"role": "user", "content": "Hello"}]},
        )

    thread = threading.Thread(target=run_chat)
    thread.start()
    assert first_attempt_started.wait(5)
    revoked = superadmin.post(
        f"/api/superadmin/licenses/{license_id}/revoke",
        headers=origin,
        json={"reason": "provider race regression"},
    )
    assert revoked.status_code == 200, revoked.text
    release_first_attempt.set()
    thread.join(5)
    assert not thread.is_alive()
    assert not groq_called.is_set()
    response = result["response"]
    assert response.status_code == 402
