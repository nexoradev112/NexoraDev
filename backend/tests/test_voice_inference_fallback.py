from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.api.voice import voice_inference_notice
from tests.test_control_plane import activate, signed_worker_post


def test_voice_inference_notice_names_missing_stt_and_tts() -> None:
    notice = voice_inference_notice(["stt", "tts"])
    assert "speech-to-text" in notice
    assert "text-to-speech" in notice
    assert "LiveKit Inference" in notice
    assert "Settings" in notice


def test_missing_tenant_stt_tts_falls_back_to_livekit_inference(tenant, superadmin, origin) -> None:
    workspace_id = tenant.post(
        "/api/auth/register",
        headers=origin,
        json={
            "email": "voice-fallback@example.com",
            "password": "Tenant-password-123!",
            "name": "Owner",
            "workspaceName": "Voice fallback",
        },
    ).json()["workspaceId"]
    now = datetime.now(UTC)
    issued = superadmin.post(
        "/api/superadmin/licenses",
        headers=origin,
        json={
            "workspaceId": workspace_id,
            "plan": "growth",
            "seats": 3,
            "validFrom": (now - timedelta(minutes=1)).isoformat(),
            "validUntil": (now + timedelta(days=30)).isoformat(),
            "providerMode": "hybrid",
            "hybridPolicy": {
                "llm": "byok",
                "stt": "byok",
                "tts": "byok",
                "realtime": "platform",
                "telephony": "byok",
            },
            "quotas": {"agents": 5, "tokens": 50_000, "voice_seconds": 5_000},
            "features": ["agents", "chat", "voice", "providers", "members"],
        },
    )
    assert issued.status_code == 201, issued.text
    activate(tenant, origin, workspace_id, issued.json()["licenseKey"])
    headers = origin | {"x-workspace-id": str(workspace_id)}
    groq = tenant.post(
        "/api/providers",
        headers=headers,
        json={"kind": "llm", "provider": "groq", "secret": "gsk_tenant_groq_key"},
    )
    assert groq.status_code == 201, groq.text
    agent = tenant.post(
        "/api/agents",
        headers=headers,
        json={
            "name": "Support FAQ",
            "channel": "voice + chat",
            "maxCallSeconds": 300,
            "providerPolicy": {
                "llm": ["openai", "anthropic"],
                "stt": ["deepgram"],
                "tts": ["elevenlabs"],
                "realtime": ["livekit"],
            },
            "workflow": {"nodes": [], "edges": []},
        },
    )
    assert agent.status_code == 201, agent.text
    token = tenant.post(
        "/api/livekit/token",
        headers=headers,
        json={"agentId": agent.json()["agent"]["id"], "sessionId": "fallbacksession"},
    )
    assert token.status_code == 200, token.text
    body = token.json()
    assert "LiveKit Inference" in body["voice_notice"]
    assert set(body["inference_fallback"]) == {"stt", "tts"}
    room = body["room_name"]
    config = signed_worker_post(tenant, "/api/internal/agents/config", {"roomName": room})
    assert config.status_code == 200, config.text
    import base64
    import json

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from tests.conftest import WORKER_KEY

    envelope = config.json()["runtimeProvidersEnvelope"]

    def decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    payload = json.loads(
        AESGCM(WORKER_KEY).decrypt(
            decode(envelope["nonce"]),
            decode(envelope["ciphertext"]),
            f"nexora-runtime-providers:v1:{room}".encode(),
        )
    )
    runtime = payload["runtimeProviders"]
    assert runtime["transport"] == "livekit_inference"
    assert runtime["llm"]["provider"] == "livekit"
    assert runtime["llm"]["inference"] is True
    assert runtime["stt"]["inference"] is True
    assert runtime["tts"]["inference"] is True
    assert "apiKey" not in runtime["llm"]
    assert "apiKey" not in runtime["stt"]
    assert "apiKey" not in runtime["tts"]
