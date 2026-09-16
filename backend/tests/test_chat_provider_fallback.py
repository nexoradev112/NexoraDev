from __future__ import annotations

import app.provider_runtime as provider_runtime
from app.provider_runtime import CompletionResult
from tests.test_control_plane import activate, issue, register


def _licensed_chat_workspace(tenant, superadmin, origin, email: str, name: str) -> tuple[int, dict[str, str]]:
    workspace_id = register(tenant, origin, email, name)
    entitlement = issue(superadmin, origin, workspace_id, "byok")
    activate(tenant, origin, workspace_id, entitlement["licenseKey"])
    return workspace_id, origin | {"x-workspace-id": str(workspace_id)}


def _create_chat_agent(tenant, headers: dict[str, str], llm_order: list[str]) -> int:
    response = tenant.post(
        "/api/agents",
        headers=headers,
        json={
            "name": "Support FAQ",
            "channel": "voice + chat",
            "providerPolicy": {
                "llm": llm_order,
                "stt": ["deepgram"],
                "tts": ["elevenlabs"],
                "realtime": ["livekit"],
            },
            "workflow": {"nodes": [], "edges": []},
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["agent"]["id"])


def test_byok_chat_uses_saved_groq_when_agent_prefers_missing_anthropic(
    tenant, superadmin, origin, monkeypatch
) -> None:
    workspace_id, headers = _licensed_chat_workspace(
        tenant, superadmin, origin, "groq-chat@example.com", "Groq chat tenant"
    )
    saved = tenant.post(
        "/api/providers",
        headers=headers,
        json={"kind": "llm", "provider": "groq", "secret": "gsk_tenant_groq_key"},
    )
    assert saved.status_code == 201, saved.text
    agent_id = _create_chat_agent(tenant, headers, ["openai", "anthropic"])
    called: list[str] = []

    def fake_openai_compatible(credential, model, messages):
        del model, messages
        called.append(credential.provider)
        return CompletionResult("hello from groq", credential.provider, "openai/gpt-oss-120b", 4, 6)

    monkeypatch.setattr(provider_runtime, "_openai_compatible", fake_openai_compatible)
    monkeypatch.setattr(
        provider_runtime,
        "_anthropic",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("anthropic must not run")),
    )

    response = tenant.post(
        "/api/chat",
        headers=headers,
        json={"agentId": agent_id, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "groq"
    assert body["text"] == "hello from groq"
    assert called == ["groq"]
    del workspace_id


def test_byok_chat_does_not_mask_groq_failure_with_missing_anthropic(
    tenant, superadmin, origin, monkeypatch
) -> None:
    _, headers = _licensed_chat_workspace(
        tenant, superadmin, origin, "groq-fail@example.com", "Groq fail tenant"
    )
    saved = tenant.post(
        "/api/providers",
        headers=headers,
        json={"kind": "llm", "provider": "groq", "secret": "gsk_tenant_groq_key"},
    )
    assert saved.status_code == 201, saved.text
    agent_id = _create_chat_agent(tenant, headers, ["openai", "anthropic", "groq"])

    def fake_openai_compatible(credential, model, messages):
        del model, messages
        raise provider_runtime.ProviderResponseError(
            f"{credential.provider} request failed",
            cost_uncertain=False,
        )

    monkeypatch.setattr(provider_runtime, "_openai_compatible", fake_openai_compatible)

    response = tenant.post(
        "/api/chat",
        headers=headers,
        json={"agentId": agent_id, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502, response.text
    assert response.json()["error"] != "Tenant anthropic credential is not configured"
    assert "did not complete" in response.json()["error"]
