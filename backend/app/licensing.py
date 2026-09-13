from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .models import License, Membership, UsageCounter, WorkspaceInvite
from .security import aware, now_utc, sha256_text

LICENSE_PREFIX = "nxlic_v1"
PROVIDER_MODES = {"byok", "platform", "hybrid"}
PROVIDER_KINDS = {"llm", "stt", "tts", "realtime", "telephony"}
VOICE_TOKEN_RESERVE_PER_SECOND = 16
DEFAULT_FEATURES = [
    "agents",
    "chat",
    "voice",
    "providers",
    "members",
    "analytics",
    "recordings",
    "telephony",
    "post_call",
]


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def signing_private_key(settings: Settings) -> Ed25519PrivateKey:
    if not settings.LICENSE_SIGNING_PRIVATE_KEY:
        raise HTTPException(status_code=503, detail="License issuer is not configured")
    try:
        raw = base64.b64decode(settings.LICENSE_SIGNING_PRIVATE_KEY, validate=True)
        return Ed25519PrivateKey.from_private_bytes(raw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="License issuer is misconfigured") from exc


def signing_public_key(settings: Settings) -> Ed25519PublicKey:
    try:
        if settings.LICENSE_SIGNING_PUBLIC_KEY:
            raw = base64.b64decode(settings.LICENSE_SIGNING_PUBLIC_KEY, validate=True)
            return Ed25519PublicKey.from_public_bytes(raw)
        return signing_private_key(settings).public_key()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="License verifier is misconfigured") from exc


def public_key_base64(settings: Settings) -> str:
    raw = signing_public_key(settings).public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode()


def sign_payload(payload: dict[str, Any], settings: Settings) -> tuple[str, str]:
    encoded_payload = _b64url_encode(canonical_payload(payload))
    signature = _b64url_encode(signing_private_key(settings).sign(canonical_payload(payload)))
    # The signed claim bytes and signature remain stored for enforcement, while
    # this independent activation secret exists only in the one-time bearer key.
    # Therefore a database read cannot reconstruct an activatable license key.
    activation_secret = _b64url_encode(secrets.token_bytes(32))
    return f"{LICENSE_PREFIX}.{encoded_payload}.{signature}.{activation_secret}", signature


def _state_payload(license_row: License) -> dict[str, Any]:
    return {
        "version": 1,
        "license_id": license_row.id,
        "workspace_id": license_row.workspace_id,
        "status": license_row.status,
        "activated_at": aware(license_row.activated_at).isoformat() if license_row.activated_at else None,
        "revoked_at": aware(license_row.revoked_at).isoformat() if license_row.revoked_at else None,
    }


def sign_license_state(license_row: License, settings: Settings) -> None:
    """Sign lifecycle state after an authorized transition."""

    if not license_row.id:
        raise ValueError("License must be flushed before signing state")
    license_row.state_signature = _b64url_encode(
        signing_private_key(settings).sign(canonical_payload(_state_payload(license_row)))
    )


def verify_license_state(license_row: License, settings: Settings) -> None:
    try:
        signing_public_key(settings).verify(
            _b64url_decode(license_row.state_signature),
            canonical_payload(_state_payload(license_row)),
        )
    except (ValueError, InvalidSignature, TypeError) as exc:
        raise HTTPException(
            status_code=402,
            detail="License lifecycle state failed integrity verification",
        ) from exc


def decode_and_verify_token(token: str, settings: Settings) -> tuple[dict[str, Any], str]:
    if len(token) > 8192:
        raise HTTPException(status_code=400, detail="License key is invalid")
    parts = token.strip().split(".")
    if len(parts) != 4 or parts[0] != LICENSE_PREFIX or len(parts[3]) < 40:
        raise HTTPException(status_code=400, detail="License key is invalid")
    try:
        payload_bytes = _b64url_decode(parts[1])
        signature_bytes = _b64url_decode(parts[2])
        signing_public_key(settings).verify(signature_bytes, payload_bytes)
        payload = json.loads(payload_bytes)
    except (ValueError, json.JSONDecodeError, InvalidSignature) as exc:
        raise HTTPException(status_code=400, detail="License signature is invalid") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise HTTPException(status_code=400, detail="License payload is unsupported")
    return payload, parts[2]


def parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise HTTPException(status_code=402, detail=f"License {label} is invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=402, detail=f"License {label} is invalid") from exc
    return aware(result)


@dataclass(frozen=True)
class LicenseClaims:
    license: License
    plan: str
    seats: int
    valid_from: datetime
    valid_until: datetime
    provider_mode: str
    hybrid_policy: dict[str, str]
    quotas: dict[str, int | None]
    features: frozenset[str]


def verified_claims(
    license_row: License,
    settings: Settings,
    *,
    require_active: bool = True,
    enforce_dates: bool = True,
) -> LicenseClaims:
    verify_license_state(license_row, settings)
    payload = license_row.signed_payload
    try:
        signing_public_key(settings).verify(_b64url_decode(license_row.signature), canonical_payload(payload))
    except (ValueError, InvalidSignature) as exc:
        raise HTTPException(status_code=402, detail="License signature is invalid") from exc

    required = {
        "license_id": license_row.id,
        "workspace_id": license_row.workspace_id,
        "plan": license_row.plan,
        "seats": license_row.seats,
        "provider_mode": license_row.provider_mode,
        "hybrid_policy": license_row.hybrid_policy,
        "quotas": license_row.quotas,
        "features": license_row.features,
        "valid_from": aware(license_row.valid_from).isoformat(),
        "valid_until": aware(license_row.valid_until).isoformat(),
    }
    if any(payload.get(key) != value for key, value in required.items()):
        raise HTTPException(status_code=402, detail="License record failed integrity verification")
    if require_active and license_row.status == "revoked":
        raise HTTPException(status_code=402, detail="License has been revoked")
    if require_active and license_row.status != "active":
        raise HTTPException(status_code=402, detail="Workspace license is not active")

    current = now_utc()
    valid_from = parse_timestamp(payload.get("valid_from"), "start date")
    valid_until = parse_timestamp(payload.get("valid_until"), "expiry")
    if enforce_dates and current < valid_from:
        raise HTTPException(status_code=402, detail="License is not valid yet")
    if enforce_dates and current >= valid_until:
        raise HTTPException(status_code=402, detail="License has expired")

    seats = payload.get("seats")
    mode = payload.get("provider_mode")
    features = payload.get("features")
    quotas = payload.get("quotas")
    hybrid = payload.get("hybrid_policy")
    if not isinstance(seats, int) or seats < 1 or mode not in PROVIDER_MODES:
        raise HTTPException(status_code=402, detail="License claims are invalid")
    if not isinstance(features, list) or not all(isinstance(item, str) for item in features):
        raise HTTPException(status_code=402, detail="License features are invalid")
    if not isinstance(quotas, dict) or not isinstance(hybrid, dict):
        raise HTTPException(status_code=402, detail="License policy is invalid")
    if mode == "hybrid" and (
        set(hybrid) != PROVIDER_KINDS or any(v not in {"byok", "platform"} for v in hybrid.values())
    ):
        raise HTTPException(status_code=402, detail="Hybrid provider policy is incomplete")
    return LicenseClaims(
        license=license_row,
        plan=str(payload.get("plan")),
        seats=seats,
        valid_from=valid_from,
        valid_until=valid_until,
        provider_mode=mode,
        hybrid_policy={str(k): str(v) for k, v in hybrid.items()},
        quotas={str(k): int(v) if v is not None else None for k, v in quotas.items()},
        features=frozenset(features),
    )


def active_license_for_workspace(
    db: Session, workspace_id: int, settings: Settings, *, lock: bool = False
) -> LicenseClaims:
    query = (
        select(License)
        .where(License.workspace_id == workspace_id, License.status == "active")
        .order_by(License.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    license_row = db.scalar(query)
    if not license_row:
        fallback = db.scalar(
            select(License)
            .where(
                License.workspace_id == workspace_id,
                License.status.in_(["revoked", "expired"]),
            )
            .order_by(License.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        if fallback:
            return verified_claims(fallback, settings)
        raise HTTPException(status_code=402, detail="Workspace requires an active license")
    return verified_claims(license_row, settings)


def effective_license_row(db: Session, workspace_id: int) -> License | None:
    """Return the effective active entitlement, or newest history for display."""

    active = db.scalar(
        select(License)
        .where(License.workspace_id == workspace_id, License.status == "active")
        .order_by(License.id.desc())
        .limit(1)
    )
    if active:
        return active
    return db.scalar(
        select(License).where(License.workspace_id == workspace_id).order_by(License.id.desc()).limit(1)
    )


def require_feature(claims: LicenseClaims, feature: str | None) -> None:
    if feature and feature not in claims.features:
        raise HTTPException(status_code=403, detail=f"Feature '{feature}' is not included in this license")


def provider_source(claims: LicenseClaims, kind: str) -> str:
    if kind not in PROVIDER_KINDS:
        raise HTTPException(status_code=400, detail="Provider kind is unsupported")
    if claims.provider_mode == "hybrid":
        # Every kind is signed into the license. There is intentionally no default.
        result = claims.hybrid_policy.get(kind)
        if result not in {"byok", "platform"}:
            raise HTTPException(status_code=403, detail=f"No provider source is licensed for {kind}")
        return result
    return claims.provider_mode


def platform_voice_token_reservation(claims: LicenseClaims, max_call_seconds: int) -> int:
    if provider_source(claims, "llm") != "platform":
        return 0
    # Until the worker reports exact usage, charge this conservative ceiling.
    return max_call_seconds * VOICE_TOKEN_RESERVE_PER_SECOND


def assert_seat_available(db: Session, claims: LicenseClaims, *, include_pending_invite: bool = True) -> None:
    members = (
        db.scalar(
            select(func.count(Membership.id)).where(Membership.workspace_id == claims.license.workspace_id)
        )
        or 0
    )
    pending = 0
    if include_pending_invite:
        pending = (
            db.scalar(
                select(func.count(WorkspaceInvite.id)).where(
                    WorkspaceInvite.workspace_id == claims.license.workspace_id,
                    WorkspaceInvite.accepted_at.is_(None),
                    WorkspaceInvite.revoked_at.is_(None),
                    WorkspaceInvite.expires_at > now_utc(),
                )
            )
            or 0
        )
    if members + pending >= claims.seats:
        raise HTTPException(status_code=403, detail="License seat limit reached")


def check_quota(db: Session, claims: LicenseClaims, unit: str, additional: int = 0) -> int:
    limit = claims.quotas.get(unit)
    if limit is None:
        return 0
    # Serialize the first-counter insert as well as later increments. Locking only
    # a counter row is insufficient when the row does not exist yet.
    db.scalar(select(License.id).where(License.id == claims.license.id).with_for_update())
    counter = db.scalar(
        select(UsageCounter)
        .where(UsageCounter.license_id == claims.license.id, UsageCounter.unit == unit)
        .with_for_update()
    )
    used = counter.used if counter else 0
    if used + additional > limit:
        raise HTTPException(status_code=402, detail=f"License {unit} quota exceeded")
    return used


def consume_quota(db: Session, claims: LicenseClaims, unit: str, quantity: int) -> None:
    if quantity < 0:
        raise ValueError("Quota consumption cannot be negative")
    check_quota(db, claims, unit, quantity)
    counter = db.scalar(
        select(UsageCounter)
        .where(UsageCounter.license_id == claims.license.id, UsageCounter.unit == unit)
        .with_for_update()
    )
    if counter:
        counter.used += quantity
    else:
        db.add(
            UsageCounter(
                workspace_id=claims.license.workspace_id,
                license_id=claims.license.id,
                unit=unit,
                used=quantity,
            )
        )


def adjust_consumed_quota(db: Session, claims: LicenseClaims, unit: str, delta: int) -> None:
    """Adjust an existing reservation while holding the entitlement lock."""

    if delta > 0:
        consume_quota(db, claims, unit, delta)
        return
    db.scalar(select(License.id).where(License.id == claims.license.id).with_for_update())
    counter = db.scalar(
        select(UsageCounter)
        .where(UsageCounter.license_id == claims.license.id, UsageCounter.unit == unit)
        .with_for_update()
    )
    if counter:
        counter.used = max(0, counter.used + delta)


def token_hash(token: str) -> str:
    return sha256_text(token.strip())


def placeholder_hash() -> str:
    return hashlib.sha256(secrets.token_bytes(48)).hexdigest()
