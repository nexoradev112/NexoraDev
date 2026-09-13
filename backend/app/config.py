from __future__ import annotations

import base64
import hmac
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    ENVIRONMENT: str = "production"
    DATABASE_URL: str = ""
    PUBLIC_BASE_URL: str = ""
    ALLOWED_ORIGINS: str = ""
    SESSION_COOKIE_NAME: str = "__Host-nexora_session"
    SESSION_TTL_HOURS: int = 24 * 7
    SESSION_IDLE_HOURS: int = 24
    SESSION_COOKIE_SECURE: bool = True
    ALLOW_PUBLIC_TENANT_REGISTRATION: bool = False
    FILE_STORAGE_ROOT: Path = Path("/var/lib/nexora/files")
    MAX_UPLOAD_BYTES: int = 25 * 1024 * 1024
    MAX_WORKSPACE_STORAGE_BYTES: int = 2 * 1024 * 1024 * 1024
    MIN_STORAGE_FREE_BYTES: int = 1024 * 1024 * 1024

    # Exactly 32 random bytes, base64 encoded. It encrypts tenant BYOK credentials.
    CREDENTIAL_MASTER_KEY: str = ""
    CREDENTIAL_KEY_VERSION: int = 1
    # Independent keyed fingerprint secret for low-entropy phone numbers.
    PHONE_HASH_KEY: str = ""

    # Ed25519 raw keys, base64 encoded. Runtime verification needs the public key;
    # only the superadmin issuer needs the private key.
    LICENSE_SIGNING_PRIVATE_KEY: str = ""
    LICENSE_SIGNING_PUBLIC_KEY: str = ""

    CALL_WORKER_TOKEN: str = ""
    # Independent AES-GCM key used only for short-lived API -> voice worker envelopes.
    WORKER_CONFIG_KEY: str = ""

    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    ELEVENLABS_API_KEY: str = ""
    DEEPGRAM_API_KEY: str = ""
    LIVEKIT_INTERNAL_URL: str = ""
    LIVEKIT_PUBLIC_URL: str = ""
    LIVEKIT_API_KEY: str = ""
    LIVEKIT_API_SECRET: str = ""
    # Kept fail-closed until an operator explicitly wires a real LiveKit SIP trunk.
    OUTBOUND_SIP_ENABLED: bool = False
    OUTBOUND_SIP_TRUNK_ID: str = ""
    # JSON map: {"<workspace-id>": {"twilio": "ST_...", ...}}. Tenant
    # input is never trusted as a trunk identifier in the shared LiveKit project.
    OUTBOUND_SIP_TRUNK_MAP_JSON: str = "{}"

    @property
    def allowed_origins(self) -> set[str]:
        return {item.strip().rstrip("/") for item in self.ALLOWED_ORIGINS.split(",") if item.strip()}

    @field_validator("ENVIRONMENT")
    @classmethod
    def environment_name(cls, value: str) -> str:
        value = value.lower().strip()
        if value not in {"development", "test", "production"}:
            raise ValueError("ENVIRONMENT must be development, test, or production")
        return value

    @model_validator(mode="after")
    def validate_security_material(self) -> Settings:
        if self.ENVIRONMENT == "test":
            return self
        if not self.DATABASE_URL.startswith("postgresql+"):
            raise ValueError("Production/development DATABASE_URL must use PostgreSQL")
        if not self.PUBLIC_BASE_URL:
            raise ValueError("PUBLIC_BASE_URL is required")
        if self.ENVIRONMENT == "production" and not self.PUBLIC_BASE_URL.startswith("https://"):
            raise ValueError("Production PUBLIC_BASE_URL must use HTTPS")
        if self.ENVIRONMENT == "production" and (
            not self.SESSION_COOKIE_SECURE or not self.SESSION_COOKIE_NAME.startswith("__Host-")
        ):
            raise ValueError("Production sessions require a Secure __Host- cookie")
        if not self.allowed_origins:
            raise ValueError("ALLOWED_ORIGINS is required")
        canonical_origins: set[str] = set()
        for origin in self.allowed_origins:
            parsed = urlparse(origin)
            if (
                origin == "*"
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
                or parsed.params
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("ALLOWED_ORIGINS must contain exact HTTP(S) origins")
            if self.ENVIRONMENT == "production" and parsed.scheme != "https":
                raise ValueError("Production ALLOWED_ORIGINS must use HTTPS")
            canonical_origins.add(f"{parsed.scheme}://{parsed.netloc}")
        public = urlparse(self.PUBLIC_BASE_URL)
        public_origin = f"{public.scheme}://{public.netloc}"
        if public_origin not in canonical_origins:
            raise ValueError("PUBLIC_BASE_URL origin must be present in ALLOWED_ORIGINS")
        if len(self.CALL_WORKER_TOKEN) < 32:
            raise ValueError("CALL_WORKER_TOKEN must contain at least 32 characters")
        try:
            worker_key = base64.urlsafe_b64decode(
                self.WORKER_CONFIG_KEY + "=" * (-len(self.WORKER_CONFIG_KEY) % 4)
            )
        except Exception as exc:  # noqa: BLE001
            raise ValueError("WORKER_CONFIG_KEY must be valid base64") from exc
        if len(worker_key) != 32:
            raise ValueError("WORKER_CONFIG_KEY must decode to exactly 32 bytes")
        try:
            key = base64.b64decode(self.CREDENTIAL_MASTER_KEY, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("CREDENTIAL_MASTER_KEY must be valid base64") from exc
        if len(key) != 32:
            raise ValueError("CREDENTIAL_MASTER_KEY must decode to exactly 32 bytes")
        try:
            phone_key = base64.b64decode(self.PHONE_HASH_KEY, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("PHONE_HASH_KEY must be valid base64") from exc
        if len(phone_key) != 32:
            raise ValueError("PHONE_HASH_KEY must decode to exactly 32 bytes")
        public_key: bytes | None = None
        if self.LICENSE_SIGNING_PUBLIC_KEY:
            try:
                public_key = base64.b64decode(self.LICENSE_SIGNING_PUBLIC_KEY, validate=True)
            except Exception as exc:  # noqa: BLE001
                raise ValueError("LICENSE_SIGNING_PUBLIC_KEY must be valid base64") from exc
            if len(public_key) != 32:
                raise ValueError("LICENSE_SIGNING_PUBLIC_KEY must decode to 32 bytes")
        elif not self.LICENSE_SIGNING_PRIVATE_KEY:
            raise ValueError("A license signing public or private key is required")
        if self.LICENSE_SIGNING_PRIVATE_KEY:
            try:
                private_key = base64.b64decode(self.LICENSE_SIGNING_PRIVATE_KEY, validate=True)
                private = Ed25519PrivateKey.from_private_bytes(private_key)
                derived_public = private.public_key().public_bytes(
                    serialization.Encoding.Raw,
                    serialization.PublicFormat.Raw,
                )
            except Exception as exc:  # noqa: BLE001
                raise ValueError("LICENSE_SIGNING_PRIVATE_KEY must decode to 32 Ed25519 bytes") from exc
            if public_key is not None and not hmac.compare_digest(public_key, derived_public):
                raise ValueError("License signing public/private keys do not match")
        if not self.FILE_STORAGE_ROOT.is_absolute():
            raise ValueError("FILE_STORAGE_ROOT must be an absolute path")
        if self.MAX_WORKSPACE_STORAGE_BYTES < self.MAX_UPLOAD_BYTES:
            raise ValueError("MAX_WORKSPACE_STORAGE_BYTES must cover at least one maximum upload")
        if self.MIN_STORAGE_FREE_BYTES < 0:
            raise ValueError("MIN_STORAGE_FREE_BYTES cannot be negative")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
