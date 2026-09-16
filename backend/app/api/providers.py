from __future__ import annotations

import json
import re
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..dependencies import WorkspaceAccess, require_recent_auth, require_workspace
from ..licensing import provider_source, require_feature
from ..models import AuditLog, CallSession, ProviderConnection
from ..provider_vault import SUPPORTED_PROVIDERS, encrypt_connection_secret, validate_provider
from ..room_termination import enqueue_room_termination
from ..security import now_utc

router = APIRouter(tags=["providers"])

TELEPHONY_PRIVATE_FIELDS: dict[str, frozenset[str]] = {
    "twilio": frozenset({"accountSid", "authToken"}),
    "vonage": frozenset({"applicationId", "privateKey", "apiKey", "apiSecret"}),
    "vobiz": frozenset({"authId", "authToken"}),
    "convox": frozenset({"bearerToken"}),
    "telnyx": frozenset({"apiKey"}),
    "cloudonix": frozenset({"domainToken"}),
    "asterisk": frozenset({"appPassword"}),
}
TELEPHONY_REQUIRED_PRIVATE_FIELDS: dict[str, frozenset[str]] = {
    "twilio": frozenset({"accountSid", "authToken"}),
    "vonage": frozenset({"applicationId", "privateKey"}),
    "vobiz": frozenset({"authId", "authToken"}),
    "convox": frozenset({"bearerToken"}),
    "telnyx": frozenset({"apiKey"}),
    "cloudonix": frozenset({"domainToken"}),
    "asterisk": frozenset({"appPassword"}),
}
TELEPHONY_PUBLIC_FIELDS: dict[str, frozenset[str]] = {
    "twilio": frozenset({"callerIds"}),
    "vonage": frozenset({"callerIds"}),
    "vobiz": frozenset({"callerIds"}),
    "convox": frozenset({"accountSid", "callerIds"}),
    "telnyx": frozenset({"connectionId", "callerIds"}),
    "cloudonix": frozenset({"domainId", "callerIds"}),
    "asterisk": frozenset({"ariEndpoint", "appName", "wsClientName", "inboundAgentId", "extensions"}),
}

GENERIC_PUBLIC_FIELDS: dict[tuple[str, str], frozenset[str]] = {
    ("llm", "openai"): frozenset({"model"}),
    ("llm", "anthropic"): frozenset({"model"}),
    ("llm", "groq"): frozenset({"model"}),
    ("stt", "deepgram"): frozenset({"model", "languageCode"}),
    ("stt", "openai"): frozenset({"model", "languageCode"}),
    ("stt", "elevenlabs"): frozenset({"model", "languageCode"}),
    ("tts", "elevenlabs"): frozenset({"model", "voiceId"}),
    ("tts", "openai"): frozenset({"model", "voice"}),
    ("realtime", "livekit"): frozenset(),
}
_SECRET_FIELD_RE = re.compile(
    r"(?:api[_-]?key|secret|password|passphrase|token|private[_-]?key|credential|auth)",
    re.IGNORECASE,
)
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:\b(?:sk|ghp|github_pat|xoxb)-[A-Za-z0-9_-]{16,}|"
    r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:api[_ -]?key|access[_ -]?token|password|client[_ -]?secret)\s*[:=])"
)
_PUBLIC_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
_LANGUAGE_CODE_RE = re.compile(r"^(?:auto|multi|[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?)$")


def _reject_public_secret(value: str) -> str:
    if _SECRET_VALUE_RE.search(value):
        raise HTTPException(status_code=422, detail="Secret material belongs in the encrypted secret field")
    return value


def _validated_generic_config(kind: str, provider: str, config: dict[str, str]) -> dict[str, str]:
    allowed = GENERIC_PUBLIC_FIELDS.get((kind, provider), frozenset())
    if any(_SECRET_FIELD_RE.search(key) for key in config):
        raise HTTPException(status_code=422, detail="Provider secrets belong in the encrypted secret field")
    if set(config) - allowed:
        raise HTTPException(status_code=422, detail="Provider config contains an unsupported field")
    normalized: dict[str, str] = {}
    for key, value in config.items():
        value = value.strip()
        if not value or len(value) > 200 or _CONTROL_CHARACTER_RE.search(value):
            raise HTTPException(status_code=422, detail="Provider config contains an invalid value")
        _reject_public_secret(value)
        if key in {"model", "voice", "voiceId"} and not _PUBLIC_IDENTIFIER_RE.fullmatch(value):
            raise HTTPException(status_code=422, detail="Provider identifier is invalid")
        if key == "languageCode" and not _LANGUAGE_CODE_RE.fullmatch(value):
            raise HTTPException(status_code=422, detail="Provider language code is invalid")
        normalized[key] = value
    return normalized


def safe_public_provider_config(connection: ProviderConnection) -> dict[str, str]:
    if connection.kind == "telephony":
        allowed = TELEPHONY_PUBLIC_FIELDS.get(connection.provider, frozenset())
    else:
        allowed = GENERIC_PUBLIC_FIELDS.get((connection.kind, connection.provider), frozenset())
    return {
        key: value
        for key, value in connection.config.items()
        if key in allowed
        and not _SECRET_FIELD_RE.search(key)
        and isinstance(value, str)
        and len(value) <= 500
        and not _CONTROL_CHARACTER_RE.search(value)
        and not _SECRET_VALUE_RE.search(value)
    }


def public_connection(connection: ProviderConnection) -> dict[str, object]:
    return {
        "id": connection.id,
        "kind": connection.kind,
        "provider": connection.provider,
        "label": connection.label,
        "config": safe_public_provider_config(connection),
        "status": connection.status,
        "configured": connection.status == "active",
        "createdAt": connection.created_at.isoformat(),
        "updatedAt": connection.updated_at.isoformat(),
    }


def _upsert_provider_connection(
    db: Session,
    settings: Settings,
    access: WorkspaceAccess,
    *,
    kind: str,
    provider: str,
    label: str,
    secret: str,
    config: dict[str, str],
) -> ProviderConnection:
    connection = db.scalar(
        select(ProviderConnection)
        .where(
            ProviderConnection.workspace_id == access.workspace.id,
            ProviderConnection.kind == kind,
            ProviderConnection.provider == provider,
        )
        .with_for_update()
    )
    if not connection:
        connection = ProviderConnection(
            workspace_id=access.workspace.id,
            kind=kind,
            provider=provider,
            label=label,
            encrypted_secret=secrets.token_urlsafe(12),
            secret_nonce=secrets.token_urlsafe(12),
            key_version=settings.CREDENTIAL_KEY_VERSION,
            config=config,
            status="active",
        )
        db.add(connection)
        db.flush()
    else:
        connection.label = label
        connection.config = config
        connection.key_version = settings.CREDENTIAL_KEY_VERSION
        connection.status = "active"
    encrypt_connection_secret(connection, secret, settings)
    return connection


@router.get("/api/providers")
def list_providers(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("admin", "providers"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    rows = db.scalars(
        select(ProviderConnection)
        .where(
            ProviderConnection.workspace_id == access.workspace.id,
            ProviderConnection.kind != "telephony",
        )
        .order_by(ProviderConnection.kind, ProviderConnection.provider)
    ).all()
    assert access.license is not None
    generic_supported = {
        kind: providers for kind, providers in SUPPORTED_PROVIDERS.items() if kind != "telephony"
    }
    sources = {kind: provider_source(access.license, kind) for kind in sorted(generic_supported)}
    return {
        "connections": [public_connection(row) for row in rows],
        "providerMode": access.license.provider_mode,
        "sources": sources,
        "supported": {key: sorted(value) for key, value in generic_supported.items()},
    }


class ProviderBody(BaseModel):
    kind: str = Field(min_length=2, max_length=32)
    provider: str = Field(min_length=2, max_length=64)
    label: str | None = Field(default=None, max_length=100)
    secret: str = Field(min_length=1, max_length=16_384)
    config: dict[str, str] = Field(default_factory=dict)


@router.post("/api/providers", status_code=201)
def save_provider(
    body: ProviderBody,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("admin", "providers"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    require_recent_auth(access)
    kind, provider = validate_provider(body.kind, body.provider)
    if kind == "telephony":
        raise HTTPException(status_code=422, detail="Use the dedicated telephony endpoint")
    config = _validated_generic_config(kind, provider, body.config)
    assert access.license is not None
    if provider_source(access.license, kind) != "byok":
        raise HTTPException(status_code=403, detail=f"License requires platform credentials for {kind}")
    connection = _upsert_provider_connection(
        db,
        settings,
        access,
        kind=kind,
        provider=provider,
        label=_reject_public_secret((body.label or provider).strip()),
        secret=body.secret,
        config=config,
    )
    db.add(
        AuditLog(
            workspace_id=access.workspace.id,
            actor=f"user:{access.user.id}",
            action="provider.saved",
            resource_type="provider_connection",
            resource_id=str(connection.id),
            details={"kind": kind, "provider": provider},
        )
    )
    db.commit()
    return {"connection": public_connection(connection)}


class TelephonyBody(BaseModel):
    provider: str = Field(min_length=2, max_length=64)
    label: str | None = Field(default=None, max_length=100)
    credentials: dict[str, str]
    config: dict[str, str] = Field(default_factory=dict)
    generate_token: bool = Field(default=False, alias="generateToken")

    @field_validator("credentials", "config")
    @classmethod
    def bounded_string_map(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not isinstance(item, str) or len(item) > 16_384 for item in value.values()):
            raise ValueError("Telephony fields must contain bounded strings")
        return {key: item.strip() for key, item in value.items() if item.strip()}


@router.post("/api/telephony", status_code=201)
def save_telephony(
    body: TelephonyBody,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("admin", "telephony"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    require_recent_auth(access)
    provider = body.provider.strip().lower()
    validate_provider("telephony", provider)
    assert access.license is not None
    if provider_source(access.license, "telephony") != "byok":
        raise HTTPException(status_code=403, detail="License requires platform telephony credentials")
    if body.generate_token:
        raise HTTPException(status_code=422, detail="Enter the credential issued by the telephony provider")
    if set(body.credentials) - TELEPHONY_PRIVATE_FIELDS[provider] or not TELEPHONY_REQUIRED_PRIVATE_FIELDS[
        provider
    ].issubset(body.credentials):
        raise HTTPException(status_code=422, detail="Telephony credentials are incomplete or unsupported")
    if set(body.config) - TELEPHONY_PUBLIC_FIELDS[provider] or any(
        len(value) > 500 for value in body.config.values()
    ):
        raise HTTPException(status_code=422, detail="Telephony config contains an unsupported field")
    if any(_SECRET_VALUE_RE.search(value) for value in body.config.values()):
        raise HTTPException(status_code=422, detail="Telephony config contains secret material")
    connection = _upsert_provider_connection(
        db,
        settings,
        access,
        kind="telephony",
        provider=provider,
        label=_reject_public_secret((body.label or provider).strip()),
        secret=json.dumps(body.credentials, separators=(",", ":"), sort_keys=True),
        config=dict(body.config),
    )
    db.add(
        AuditLog(
            workspace_id=access.workspace.id,
            actor=f"user:{access.user.id}",
            action="telephony.saved",
            resource_type="provider_connection",
            resource_id=str(connection.id),
            details={"provider": provider},
        )
    )
    db.commit()
    return {"connection": public_connection(connection)}


def _disable_provider_connection(
    connection_id: int,
    access: WorkspaceAccess,
    db: Session,
    settings: Settings,
) -> dict[str, object]:
    require_recent_auth(access)
    connection = db.scalar(
        select(ProviderConnection)
        .where(
            ProviderConnection.id == connection_id,
            ProviderConnection.workspace_id == access.workspace.id,
        )
        .with_for_update()
    )
    if not connection:
        raise HTTPException(status_code=404, detail="Provider connection not found")
    assert access.license is not None
    require_feature(access.license, "telephony" if connection.kind == "telephony" else "providers")
    connection.status = "disabled"
    # Cryptographic erasure: replace, rather than retain, the previous ciphertext.
    encrypt_connection_secret(connection, secrets.token_urlsafe(48), settings)
    calls = db.scalars(
        select(CallSession)
        .where(
            CallSession.workspace_id == access.workspace.id,
            CallSession.status.in_(["queued", "dialing", "active"]),
        )
        .order_by(CallSession.id)
        .with_for_update()
    ).all()
    canceled_calls = 0
    for call in calls:
        snapshot = call.details.get("agentRuntimeSnapshot")
        policy = snapshot.get("providerPolicy") if isinstance(snapshot, dict) else None
        provider_order = policy.get(connection.kind) if isinstance(policy, dict) else None
        uses_connection = (
            call.provider == connection.provider
            if connection.kind == "telephony"
            else isinstance(provider_order, list) and connection.provider in provider_order
        )
        if not uses_connection:
            continue
        previous_status = call.status
        call.status = "canceled"
        call.ended_at = now_utc()
        call.reserved_voice_seconds = 0
        call.reserved_tokens = 0
        call.reservation_expires_at = None
        call.details = {
            **call.details,
            "canceledByProviderDisable": True,
            "canceledFromStatus": previous_status,
            "disabledProviderKind": connection.kind,
            "disabledProvider": connection.provider,
        }
        enqueue_room_termination(db, call, "provider_disabled")
        canceled_calls += 1
    db.add(
        AuditLog(
            workspace_id=access.workspace.id,
            actor=f"user:{access.user.id}",
            action="provider.disabled",
            resource_type="provider_connection",
            resource_id=str(connection.id),
            details={
                "kind": connection.kind,
                "provider": connection.provider,
                "canceledCalls": canceled_calls,
            },
        )
    )
    db.commit()
    return {"disabled": True, "id": connection.id, "canceledCalls": canceled_calls}


@router.delete("/api/providers")
def disable_provider(
    id: Annotated[int, Query(ge=1)],
    access: Annotated[WorkspaceAccess, Depends(require_workspace("admin"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return _disable_provider_connection(id, access, db, settings)


@router.delete("/api/providers/{connection_id}")
def disable_provider_path(
    connection_id: Annotated[int, Path(ge=1)],
    access: Annotated[WorkspaceAccess, Depends(require_workspace("admin"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return _disable_provider_connection(connection_id, access, db, settings)
