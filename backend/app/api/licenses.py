from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..dependencies import (
    Principal,
    WorkspaceAccess,
    require_recent_auth,
    require_superadmin,
    require_workspace_unlicensed,
)
from ..licensing import (
    DEFAULT_FEATURES,
    PROVIDER_KINDS,
    decode_and_verify_token,
    effective_license_row,
    placeholder_hash,
    sign_license_state,
    sign_payload,
    token_hash,
    verified_claims,
)
from ..models import (
    AuditLog,
    CallSession,
    License,
    LiveKitRoomTerminationJob,
    Membership,
    Workspace,
)
from ..room_termination import enqueue_room_termination, serialize_termination_job
from ..security import aware, now_utc

router = APIRouter(tags=["licenses"])


def serialize_license(row: License, settings: Settings) -> dict[str, object]:
    claims = verified_claims(row, settings, require_active=False, enforce_dates=False)
    current = now_utc()
    status = row.status
    if status != "revoked" and current >= claims.valid_until:
        status = "expired"
    elif status == "active" and current < claims.valid_from:
        status = "not_yet_valid"
    return {
        "id": row.id,
        "workspaceId": row.workspace_id,
        "keyPrefix": row.token_prefix,
        "plan": claims.plan,
        "seats": claims.seats,
        "validFrom": claims.valid_from.isoformat(),
        "validUntil": claims.valid_until.isoformat(),
        "status": status,
        "providerMode": claims.provider_mode,
        "hybridPolicy": claims.hybrid_policy,
        "quotas": claims.quotas,
        "features": sorted(claims.features),
        "activatedAt": row.activated_at.isoformat() if row.activated_at else None,
        "revokedAt": row.revoked_at.isoformat() if row.revoked_at else None,
    }


@router.get("/api/licenses/current")
def current_license(
    access: Annotated[WorkspaceAccess, Depends(require_workspace_unlicensed("viewer"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object | None]:
    row = effective_license_row(db, access.workspace.id)
    return {"license": serialize_license(row, settings) if row else None}


class ActivateBody(BaseModel):
    license_key: str = Field(min_length=100, max_length=8192, alias="licenseKey")


@router.post("/api/licenses/activate")
def activate_license(
    body: ActivateBody,
    access: Annotated[WorkspaceAccess, Depends(require_workspace_unlicensed("admin"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    payload, signature = decode_and_verify_token(body.license_key, settings)
    if payload.get("workspace_id") != access.workspace.id:
        raise HTTPException(status_code=403, detail="License belongs to a different workspace")
    # Lock both entitlement and tenant. This prevents competing activation and
    # membership changes from racing with the transition.
    db.scalar(select(Workspace).where(Workspace.id == access.workspace.id).with_for_update())
    row = db.scalar(
        select(License).where(License.token_hash == token_hash(body.license_key)).with_for_update()
    )
    if not row or row.workspace_id != access.workspace.id:
        raise HTTPException(status_code=400, detail="License key is invalid")
    if row.signed_payload != payload or row.signature != signature:
        raise HTTPException(status_code=400, detail="License key failed integrity verification")
    claims = verified_claims(row, settings, require_active=False, enforce_dates=True)
    member_count = (
        db.scalar(select(func.count(Membership.id)).where(Membership.workspace_id == access.workspace.id))
        or 0
    )
    if member_count > claims.seats:
        raise HTTPException(status_code=403, detail="Workspace already exceeds this license seat limit")
    other_active_rows = db.scalars(
        select(License)
        .where(
            License.workspace_id == access.workspace.id,
            License.status == "active",
            License.id != row.id,
        )
        .with_for_update()
    ).all()
    for other in other_active_rows:
        other_claims = verified_claims(other, settings, require_active=False, enforce_dates=False)
        if now_utc() >= other_claims.valid_until:
            other.status = "expired"
            sign_license_state(other, settings)
            continue
        raise HTTPException(status_code=409, detail="Revoke the current license before activating another")
    if row.status == "revoked":
        raise HTTPException(status_code=402, detail="License has been revoked")
    if row.status not in {"unused", "active"}:
        raise HTTPException(status_code=402, detail="License cannot be activated")
    if row.status == "unused":
        row.status = "active"
        row.activated_at = now_utc()
        row.activated_by_user_id = access.user.id
        sign_license_state(row, settings)
        access.workspace.status = "active"
        db.add(
            AuditLog(
                workspace_id=access.workspace.id,
                actor=f"user:{access.user.id}",
                action="license.activated",
                resource_type="license",
                resource_id=str(row.id),
            )
        )
        db.commit()
    return {"license": serialize_license(row, settings), "activated": True}


class IssueLicenseBody(BaseModel):
    workspace_id: int = Field(ge=1, alias="workspaceId")
    plan: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    seats: int = Field(ge=1, le=100_000)
    valid_from: datetime = Field(alias="validFrom")
    valid_until: datetime = Field(alias="validUntil")
    provider_mode: Literal["byok", "platform", "hybrid"] = Field(alias="providerMode")
    hybrid_policy: dict[str, Literal["byok", "platform"]] = Field(default_factory=dict, alias="hybridPolicy")
    quotas: dict[str, int | None] = Field(default_factory=dict)
    features: list[str] = Field(default_factory=lambda: list(DEFAULT_FEATURES))

    @field_validator("valid_from", "valid_until")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("License dates must include a timezone")
        return value.astimezone(UTC)

    @field_validator("features")
    @classmethod
    def validate_features(cls, value: list[str]) -> list[str]:
        cleaned = sorted({item.strip() for item in value if item.strip()})
        if not cleaned or any(len(item) > 64 for item in cleaned):
            raise ValueError("At least one valid feature is required")
        return cleaned

    @field_validator("quotas")
    @classmethod
    def validate_quotas(cls, value: dict[str, int | None]) -> dict[str, int | None]:
        if set(value) - {"voice_seconds", "tokens", "agents"}:
            raise ValueError("Only voice_seconds, tokens, and agents quotas are supported")
        if any(item is not None and (not isinstance(item, int) or item < 0) for item in value.values()):
            raise ValueError("Quotas must be non-negative integers or null")
        return value

    @model_validator(mode="after")
    def validate_policy_and_dates(self) -> IssueLicenseBody:
        if self.valid_until <= self.valid_from:
            raise ValueError("validUntil must be after validFrom")
        if self.provider_mode == "hybrid":
            if set(self.hybrid_policy) != PROVIDER_KINDS:
                raise ValueError("Hybrid policy must explicitly map every provider kind")
        elif self.hybrid_policy:
            raise ValueError("hybridPolicy is only allowed for hybrid licenses")
        if "voice" in self.features:
            realtime_source = (
                self.hybrid_policy.get("realtime") if self.provider_mode == "hybrid" else self.provider_mode
            )
            if realtime_source != "platform":
                raise ValueError("Voice licenses on the single-worker droplet must map realtime to platform")
        return self


@router.get("/api/superadmin/licenses")
def list_all_licenses(
    _principal: Annotated[Principal, Depends(require_superadmin)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    rows = db.scalars(select(License).order_by(License.id.desc()).limit(500)).all()
    return {"licenses": [serialize_license(row, settings) for row in rows]}


@router.post("/api/superadmin/licenses", status_code=201)
def issue_license(
    body: IssueLicenseBody,
    principal: Annotated[Principal, Depends(require_superadmin)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    require_recent_auth(principal)
    workspace = db.scalar(select(Workspace).where(Workspace.id == body.workspace_id).with_for_update())
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    row = License(
        workspace_id=workspace.id,
        token_hash=placeholder_hash(),
        token_prefix=LICENSE_DISPLAY_PREFIX,
        plan=body.plan,
        seats=body.seats,
        valid_from=body.valid_from,
        valid_until=body.valid_until,
        status="unused",
        provider_mode=body.provider_mode,
        hybrid_policy=dict(body.hybrid_policy),
        quotas=dict(body.quotas),
        features=list(body.features),
        signed_payload={},
        signature="pending",
        state_signature="pending",
    )
    db.add(row)
    db.flush()
    payload = {
        "version": 1,
        "license_id": row.id,
        "workspace_id": workspace.id,
        "plan": body.plan,
        "seats": body.seats,
        "valid_from": aware(body.valid_from).isoformat(),
        "valid_until": aware(body.valid_until).isoformat(),
        "provider_mode": body.provider_mode,
        "hybrid_policy": dict(body.hybrid_policy),
        "quotas": dict(body.quotas),
        "features": list(body.features),
    }
    key, signature = sign_payload(payload, settings)
    row.signed_payload = payload
    row.signature = signature
    sign_license_state(row, settings)
    row.token_hash = token_hash(key)
    row.token_prefix = key[:20]
    db.add(
        AuditLog(
            workspace_id=workspace.id,
            actor=f"superadmin:{principal.user.id}",
            action="license.issued",
            resource_type="license",
            resource_id=str(row.id),
            details={"plan": body.plan, "seats": body.seats, "providerMode": body.provider_mode},
        )
    )
    db.commit()
    # `licenseKey` is intentionally present only in this response.
    return {"license": serialize_license(row, settings), "licenseKey": key}


class RevokeBody(BaseModel):
    reason: str = Field(default="revoked by superadmin", min_length=3, max_length=500)


@router.post("/api/superadmin/licenses/{license_id}/revoke")
def revoke_license(
    license_id: int,
    body: RevokeBody,
    principal: Annotated[Principal, Depends(require_superadmin)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    require_recent_auth(principal)
    workspace_id = db.scalar(select(License.workspace_id).where(License.id == license_id))
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="License not found")
    workspace = db.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update())
    license_rows = db.scalars(
        select(License).where(License.workspace_id == workspace_id).order_by(License.id).with_for_update()
    ).all()
    row = next((item for item in license_rows if item.id == license_id), None)
    if not row:
        raise HTTPException(status_code=404, detail="License not found")
    effective_active_id = next(
        (item.id for item in reversed(license_rows) if item.status == "active"),
        None,
    )
    revoked_effective_entitlement = row.status == "active" and row.id == effective_active_id
    if row.status != "revoked":
        row.status = "revoked"
        row.revoked_at = now_utc()
        row.revoked_by_user_id = principal.user.id
        sign_license_state(row, settings)
        canceled_calls = 0
        if revoked_effective_entitlement and workspace:
            workspace.status = "pending_license"
            calls = db.scalars(
                select(CallSession)
                .where(
                    CallSession.workspace_id == row.workspace_id,
                    CallSession.license_id == row.id,
                    CallSession.status.in_(["queued", "dialing", "active"]),
                )
                .order_by(CallSession.id)
                .with_for_update()
            ).all()
            ended_at = now_utc()
            for call in calls:
                previous_status = call.status
                call.status = "canceled"
                call.ended_at = ended_at
                call.reserved_voice_seconds = 0
                call.reserved_tokens = 0
                call.reservation_expires_at = None
                call.details = {
                    **call.details,
                    "canceledByLicenseRevocation": True,
                    "canceledLicenseId": row.id,
                    "canceledFromStatus": previous_status,
                }
                enqueue_room_termination(db, call, "license_revoked")
            canceled_calls = len(calls)
        db.add(
            AuditLog(
                workspace_id=row.workspace_id,
                actor=f"superadmin:{principal.user.id}",
                action="license.revoked",
                resource_type="license",
                resource_id=str(row.id),
                details={
                    "reason": body.reason,
                    "revokedEffectiveEntitlement": revoked_effective_entitlement,
                    "canceledCalls": canceled_calls,
                },
            )
        )
        db.commit()
    return {"license": serialize_license(row, settings), "revoked": True}


@router.get("/api/superadmin/room-terminations")
def list_room_terminations(
    principal: Annotated[Principal, Depends(require_superadmin)],
    db: Annotated[Session, Depends(get_db)],
    status: Annotated[Literal["pending", "running", "succeeded"] | None, Query()] = None,
    workspace_id: Annotated[int | None, Query(alias="workspaceId", ge=1)] = None,
    alerted_only: Annotated[bool, Query(alias="alertedOnly")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> dict[str, object]:
    require_recent_auth(principal)
    query = select(LiveKitRoomTerminationJob)
    if status:
        query = query.where(LiveKitRoomTerminationJob.status == status)
    if workspace_id is not None:
        query = query.where(LiveKitRoomTerminationJob.workspace_id == workspace_id)
    if alerted_only:
        query = query.where(LiveKitRoomTerminationJob.alerted_at.is_not(None))
    jobs = db.scalars(query.order_by(LiveKitRoomTerminationJob.updated_at.desc()).limit(limit)).all()
    return {"jobs": [serialize_termination_job(job) for job in jobs]}


# Used only before a one-time key exists; overwritten before the transaction commits.
LICENSE_DISPLAY_PREFIX = "nxlic_v1.pending"
