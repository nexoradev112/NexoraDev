from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .licensing import LicenseClaims, provider_source
from .models import ProviderConnection

SUPPORTED_PROVIDERS: dict[str, set[str]] = {
    "llm": {"openai", "anthropic", "groq"},
    "stt": {"deepgram", "openai", "elevenlabs"},
    "tts": {"elevenlabs", "openai"},
    "realtime": {"livekit"},
    "telephony": {
        "twilio",
        "vonage",
        "vobiz",
        "convox",
        "telnyx",
        "cloudonix",
        "asterisk",
    },
}

# Server-funded models are deliberately bounded so a tenant cannot select an
# unexpectedly expensive model in a saved graph and charge the platform account.
GROQ_DEFAULT_MODEL = "openai/gpt-oss-120b"
GROQ_RETIRED_MODELS = {
    "llama-3.3-70b-versatile": "openai/gpt-oss-120b",
    "llama-3.1-8b-instant": "openai/gpt-oss-20b",
}
GROQ_CURRENT_MODELS = frozenset(
    {
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.6-27b",
        *GROQ_RETIRED_MODELS.values(),
    }
)
GROQ_NATIVE_PREFIXES = ("llama", "mixtral", "gemma", "openai/", "qwen/", "meta-llama/")

PLATFORM_MODEL_ALLOWLIST: dict[str, dict[str, frozenset[str]]] = {
    "llm": {
        "openai": frozenset({"gpt-4.1-mini", "gpt-4o-mini", "gpt-5-mini"}),
        "anthropic": frozenset({"claude-3-5-haiku-latest"}),
        "groq": GROQ_CURRENT_MODELS,
    },
    "stt": {
        "openai": frozenset({"gpt-4o-mini-transcribe"}),
        "deepgram": frozenset({"nova-3"}),
        "elevenlabs": frozenset({"scribe_v2_realtime"}),
    },
    "tts": {
        "openai": frozenset({"gpt-4o-mini-tts"}),
        "elevenlabs": frozenset({"eleven_flash_v2_5"}),
    },
}


def resolve_groq_model(requested: str, configured: str = "") -> str:
    candidate = requested.strip()
    if not candidate.startswith(GROQ_NATIVE_PREFIXES):
        candidate = configured.strip() or GROQ_DEFAULT_MODEL
    return GROQ_RETIRED_MODELS.get(candidate, candidate)


def enforce_platform_model(kind: str, provider: str, model: str, source: str) -> str:
    if source != "platform":
        return model
    allowed = PLATFORM_MODEL_ALLOWLIST.get(kind, {}).get(provider, frozenset())
    if model not in allowed:
        raise HTTPException(status_code=403, detail=f"Platform-funded {provider} model is not allowed")
    return model


@dataclass(frozen=True)
class RuntimeCredential:
    source: str
    kind: str
    provider: str
    secret: str
    config: dict[str, str]


def validate_provider(kind: str, provider: str) -> tuple[str, str]:
    kind = kind.strip().lower()
    provider = provider.strip().lower()
    if kind not in SUPPORTED_PROVIDERS or provider not in SUPPORTED_PROVIDERS[kind]:
        raise HTTPException(status_code=422, detail="Provider is unsupported for this capability")
    return kind, provider


def _master_key(settings: Settings) -> bytes:
    try:
        key = base64.b64decode(settings.CREDENTIAL_MASTER_KEY, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="Credential vault is unavailable") from exc
    if len(key) != 32:
        raise HTTPException(status_code=503, detail="Credential vault is unavailable")
    return key


def _aad(connection: ProviderConnection) -> bytes:
    if not connection.id:
        raise ValueError("Provider record must be flushed before encryption")
    return (
        f"provider-credential|w={connection.workspace_id}|kind={connection.kind}|provider={connection.provider}"
        f"|record={connection.id}|v={connection.key_version}"
    ).encode()


def encrypt_connection_secret(connection: ProviderConnection, secret: str, settings: Settings) -> None:
    if not secret or len(secret) > 16_384:
        raise HTTPException(status_code=422, detail="Provider credential must contain 1-16384 characters")
    nonce = os.urandom(12)
    encrypted = AESGCM(_master_key(settings)).encrypt(nonce, secret.encode(), _aad(connection))
    connection.secret_nonce = base64.b64encode(nonce).decode()
    connection.encrypted_secret = base64.b64encode(encrypted).decode()


def decrypt_connection_secret(connection: ProviderConnection, settings: Settings) -> str:
    try:
        nonce = base64.b64decode(connection.secret_nonce, validate=True)
        encrypted = base64.b64decode(connection.encrypted_secret, validate=True)
        return AESGCM(_master_key(settings)).decrypt(nonce, encrypted, _aad(connection)).decode()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        # Never include ciphertext/provider credentials in the error.
        raise HTTPException(status_code=503, detail="Stored provider credential cannot be decrypted") from exc


def platform_credential(kind: str, provider: str, settings: Settings) -> RuntimeCredential:
    kind, provider = validate_provider(kind, provider)
    secret = ""
    config: dict[str, str] = {}
    if kind == "llm" and provider == "openai":
        secret = settings.OPENAI_API_KEY
        config = {"base_url": "https://api.openai.com/v1"}
    elif kind == "llm" and provider == "anthropic":
        secret = settings.ANTHROPIC_API_KEY
        config = {"base_url": "https://api.anthropic.com/v1"}
    elif kind == "llm" and provider == "groq":
        secret = settings.GROQ_API_KEY
        config = {"base_url": "https://api.groq.com/openai/v1"}
    elif kind == "tts" and provider == "elevenlabs":
        secret = settings.ELEVENLABS_API_KEY
        config = {"base_url": "https://api.elevenlabs.io/v1"}
    elif kind == "tts" and provider == "openai":
        secret = settings.OPENAI_API_KEY
        config = {"base_url": "https://api.openai.com/v1"}
    elif kind == "stt" and provider == "deepgram":
        secret = settings.DEEPGRAM_API_KEY
        config = {"base_url": "https://api.deepgram.com/v1"}
    elif kind == "stt" and provider == "openai":
        secret = settings.OPENAI_API_KEY
        config = {"base_url": "https://api.openai.com/v1"}
    elif kind == "stt" and provider == "elevenlabs":
        secret = settings.ELEVENLABS_API_KEY
        config = {"base_url": "https://api.elevenlabs.io/v1"}
    elif kind == "realtime" and provider == "livekit":
        secret = settings.LIVEKIT_API_SECRET
        config = {
            "internal_url": settings.LIVEKIT_INTERNAL_URL,
            "public_url": settings.LIVEKIT_PUBLIC_URL,
            "api_key": settings.LIVEKIT_API_KEY,
        }
    else:
        # Telephony credentials vary and must be supplied as one JSON document in a
        # dedicated server environment variable. We deliberately never infer it.
        env_name = f"{provider.upper()}_CREDENTIALS_JSON"
        secret = os.environ.get(env_name, "")
        config = {"format": "json"}
    if not secret or (
        kind == "realtime"
        and (not config.get("internal_url") or not config.get("public_url") or not config.get("api_key"))
    ):
        raise HTTPException(status_code=503, detail=f"Platform {provider} credential is not configured")
    return RuntimeCredential("platform", kind, provider, secret, config)


def byok_credential(
    db: Session,
    workspace_id: int,
    kind: str,
    provider: str,
    settings: Settings,
) -> RuntimeCredential:
    kind, provider = validate_provider(kind, provider)
    connection = db.scalar(
        select(ProviderConnection).where(
            ProviderConnection.workspace_id == workspace_id,
            ProviderConnection.kind == kind,
            ProviderConnection.provider == provider,
            ProviderConnection.status == "active",
        )
    )
    if not connection:
        raise HTTPException(status_code=503, detail=f"Tenant {provider} credential is not configured")
    return RuntimeCredential(
        source="byok",
        kind=kind,
        provider=provider,
        secret=decrypt_connection_secret(connection, settings),
        config=dict(connection.config),
    )


def resolve_runtime_credential(
    db: Session,
    claims: LicenseClaims,
    kind: str,
    provider: str,
    settings: Settings,
) -> RuntimeCredential:
    source = provider_source(claims, kind)
    if source == "byok":
        return byok_credential(db, claims.license.workspace_id, kind, provider, settings)
    if source == "platform":
        return platform_credential(kind, provider, settings)
    # provider_source already validates this; the explicit guard prevents future
    # changes from introducing a silent fallback.
    raise HTTPException(status_code=403, detail=f"No licensed credential source for {kind}")
