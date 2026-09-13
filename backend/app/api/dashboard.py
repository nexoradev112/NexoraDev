from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import hmac
import io
import json
import re
import uuid
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from datetime import date as Date
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..dependencies import WorkspaceAccess, require_workspace
from ..licensing import (
    active_license_for_workspace,
    adjust_consumed_quota,
    check_quota,
    consume_quota,
    platform_voice_token_reservation,
    provider_source,
    require_feature,
    verified_claims,
)
from ..livekit_urls import exact_livekit_origin
from ..models import (
    Agent,
    AuditLog,
    CallSession,
    ConsentRecord,
    License,
    Membership,
    PhoneNumber,
    ProviderConnection,
    SuppressionEntry,
    UsageCounter,
    UsageEvent,
    Workspace,
)
from ..postcall import snapshot_post_call_plan
from ..provider_vault import resolve_runtime_credential
from ..room_termination import enqueue_room_termination
from ..security import aware, now_utc
from .providers import safe_public_provider_config
from .voice import call_agent_snapshot, resolve_agent_runtime_providers, runtime_agent_snapshot

router = APIRouter(tags=["dashboard"])
E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def call_json(call: CallSession) -> dict[str, object]:
    return {
        "id": call.id,
        "agentId": call.agent_id,
        "roomName": call.room_name,
        "direction": call.direction,
        "status": call.status,
        "durationSeconds": call.duration_seconds,
        "summary": call.summary,
        "sentiment": call.sentiment,
        "disposition": call.disposition,
        "pipelineCompleted": call.pipeline_completed,
        "transferCount": call.transfer_count,
        "creditsUsed": call.credits_used,
        "costMicros": call.cost_micros,
        "postCall": dict(call.details).get("postCall", {}),
        "createdAt": call.created_at.isoformat(),
        "endedAt": call.ended_at.isoformat() if call.ended_at else None,
    }


def stats(db: Session, access: WorkspaceAccess) -> dict[str, int]:
    workspace_id = access.workspace.id
    assert access.license is not None
    total_agents = (
        db.scalar(
            select(func.count(Agent.id)).where(Agent.workspace_id == workspace_id, Agent.status != "archived")
        )
        or 0
    )
    total_calls = (
        db.scalar(select(func.count(CallSession.id)).where(CallSession.workspace_id == workspace_id)) or 0
    )
    live_calls = (
        db.scalar(
            select(func.count(CallSession.id)).where(
                CallSession.workspace_id == workspace_id,
                CallSession.status == "active",
                CallSession.reservation_expires_at > now_utc(),
            )
        )
        or 0
    )
    voice_seconds = (
        db.scalar(
            select(func.coalesce(func.sum(CallSession.duration_seconds), 0)).where(
                CallSession.workspace_id == workspace_id
            )
        )
        or 0
    )
    token_counter = (
        db.scalar(
            select(UsageCounter.used).where(
                UsageCounter.license_id == access.license.license.id,
                UsageCounter.unit == "tokens",
            )
        )
        or 0
    )
    seats_used = (
        db.scalar(select(func.count(Membership.id)).where(Membership.workspace_id == workspace_id)) or 0
    )
    return {
        "totalAgents": int(total_agents),
        "totalCalls": int(total_calls),
        "liveCalls": int(live_calls),
        "voiceSeconds": int(voice_seconds),
        "tokensUsed": int(token_counter),
        "seatsUsed": int(seats_used),
        "seatsLimit": access.license.seats,
    }


@router.get("/api/dashboard")
def dashboard(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    agents = db.scalars(
        select(Agent)
        .where(Agent.workspace_id == access.workspace.id)
        .order_by(Agent.updated_at.desc())
        .limit(5)
    ).all()
    calls = db.scalars(
        select(CallSession)
        .where(CallSession.workspace_id == access.workspace.id)
        .order_by(CallSession.created_at.desc())
        .limit(10)
    ).all()
    return {
        "workspace": {
            "id": access.workspace.id,
            "name": access.workspace.name,
            "role": access.membership.role,
        },
        "stats": stats(db, access),
        "agents": [{"id": item.id, "name": item.name, "status": item.status} for item in agents],
        "recentCalls": [call_json(item) for item in calls],
    }


@router.get("/api/calls")
def calls(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer", "analytics"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    rows = db.scalars(
        select(CallSession)
        .where(CallSession.workspace_id == access.workspace.id)
        .order_by(CallSession.created_at.desc())
        .limit(200)
    ).all()
    return {"calls": [call_json(row) for row in rows]}


def _phone_fingerprint(settings: Settings, workspace_id: int, phone: str) -> str:
    try:
        key = base64.b64decode(settings.PHONE_HASH_KEY, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="Compliance fingerprint service is unavailable") from exc
    return hmac.new(key, f"phone|w={workspace_id}|{phone}".encode(), hashlib.sha256).hexdigest()


class ComplianceBody(BaseModel):
    type: Literal["consent", "suppression"]
    phone_number: str = Field(min_length=9, max_length=16, alias="phoneNumber")
    channel: Literal["voice", "recording"] = "voice"
    status: Literal["granted", "revoked"] = "granted"
    legal_basis: Literal["consent", "contract", "legitimate-interest"] = Field(
        default="consent", alias="legalBasis"
    )
    evidence_ref: str = Field(default="", max_length=200, alias="evidenceRef")
    reason: Literal["do-not-call", "complaint", "legal", "manual"] = "do-not-call"


def _consent_json(record: ConsentRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "channel": record.channel,
        "status": record.status,
        "legalBasis": record.legal_basis,
        "evidenceRef": record.evidence_ref,
        "capturedAt": record.captured_at.isoformat(),
        "expiresAt": record.expires_at.isoformat() if record.expires_at else None,
    }


def _suppression_json(record: SuppressionEntry) -> dict[str, object]:
    return {
        "id": record.id,
        "reason": record.reason,
        "source": record.source,
        "expiresAt": record.expires_at.isoformat() if record.expires_at else None,
        "createdAt": record.created_at.isoformat(),
    }


def _cancel_calls_for_compliance(
    db: Session,
    workspace_id: int,
    phone_number: str,
    reason: str,
    *,
    recording_only: bool = False,
) -> list[str]:
    calls = db.scalars(
        select(CallSession)
        .where(
            CallSession.workspace_id == workspace_id,
            CallSession.direction == "outbound",
            CallSession.to_number == phone_number,
            CallSession.status.in_(["queued", "dialing", "active"]),
        )
        .order_by(CallSession.id)
        .with_for_update()
    ).all()
    rooms: list[str] = []
    ended_at = now_utc()
    for call in calls:
        snapshot = call.details.get("agentRuntimeSnapshot")
        if recording_only and not (isinstance(snapshot, dict) and snapshot.get("recordingEnabled") is True):
            continue
        previous_status = call.status
        call.status = "canceled"
        call.ended_at = ended_at
        call.reserved_voice_seconds = 0
        call.reserved_tokens = 0
        call.reservation_expires_at = None
        call.details = {
            **call.details,
            "canceledByCompliance": reason,
            "canceledFromStatus": previous_status,
        }
        enqueue_room_termination(db, call, reason)
        rooms.append(call.room_name)
    return rooms


@router.get("/api/compliance")
def compliance(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("operator", "telephony"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    consents = db.scalars(
        select(ConsentRecord)
        .where(ConsentRecord.workspace_id == access.workspace.id)
        .order_by(ConsentRecord.captured_at.desc())
        .limit(200)
    ).all()
    suppressions = db.scalars(
        select(SuppressionEntry)
        .where(SuppressionEntry.workspace_id == access.workspace.id)
        .order_by(SuppressionEntry.created_at.desc())
        .limit(200)
    ).all()
    return {
        "consents": [_consent_json(record) for record in consents],
        "suppressions": [_suppression_json(record) for record in suppressions],
    }


@router.post("/api/compliance", status_code=201)
def save_compliance(
    body: ComplianceBody,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("operator", "telephony"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    phone_number = body.phone_number.strip()
    if not E164_RE.fullmatch(phone_number):
        raise HTTPException(status_code=422, detail="phoneNumber must use E.164 format")
    phone_hash = _phone_fingerprint(settings, access.workspace.id, phone_number)
    if body.type == "consent":
        evidence = body.evidence_ref.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,199}", evidence):
            raise HTTPException(status_code=422, detail="evidenceRef is invalid")
        record = db.scalar(
            select(ConsentRecord)
            .where(
                ConsentRecord.workspace_id == access.workspace.id,
                ConsentRecord.phone_hash == phone_hash,
                ConsentRecord.channel == body.channel,
            )
            .with_for_update()
        )
        if not record:
            record = ConsentRecord(
                workspace_id=access.workspace.id,
                phone_hash=phone_hash,
                channel=body.channel,
                status=body.status,
                legal_basis=body.legal_basis,
                evidence_ref=evidence,
                captured_at=now_utc(),
                created_by_user_id=access.user.id,
            )
            db.add(record)
        else:
            record.status = body.status
            record.legal_basis = body.legal_basis
            record.evidence_ref = evidence
            record.captured_at = now_utc()
            record.created_by_user_id = access.user.id
        db.flush()
        db.add(
            AuditLog(
                workspace_id=access.workspace.id,
                actor=f"user:{access.user.id}",
                action="compliance.consent_saved",
                resource_type="consent_record",
                resource_id=str(record.id),
                details={"channel": body.channel, "status": body.status},
            )
        )
        rooms = (
            _cancel_calls_for_compliance(
                db,
                access.workspace.id,
                phone_number,
                f"{body.channel}_consent_revoked",
                recording_only=body.channel == "recording",
            )
            if body.status == "revoked"
            else []
        )
        db.commit()
        return {"consent": _consent_json(record), "canceledCalls": len(rooms)}

    record = db.scalar(
        select(SuppressionEntry)
        .where(
            SuppressionEntry.workspace_id == access.workspace.id,
            SuppressionEntry.phone_hash == phone_hash,
        )
        .with_for_update()
    )
    if not record:
        record = SuppressionEntry(
            workspace_id=access.workspace.id,
            phone_hash=phone_hash,
            reason=body.reason,
            source="manual",
            created_by_user_id=access.user.id,
        )
        db.add(record)
    else:
        record.reason = body.reason
        record.source = "manual"
        record.created_by_user_id = access.user.id
        record.created_at = now_utc()
    db.flush()
    db.add(
        AuditLog(
            workspace_id=access.workspace.id,
            actor=f"user:{access.user.id}",
            action="compliance.suppression_saved",
            resource_type="suppression_entry",
            resource_id=str(record.id),
            details={"reason": body.reason},
        )
    )
    rooms = _cancel_calls_for_compliance(
        db,
        access.workspace.id,
        phone_number,
        "suppression_added",
    )
    db.commit()
    return {"suppression": _suppression_json(record), "canceledCalls": len(rooms)}


def _require_current_consent(
    db: Session,
    settings: Settings,
    workspace_id: int,
    phone: str,
    channel: str,
) -> None:
    phone_hash = _phone_fingerprint(settings, workspace_id, phone)
    current = now_utc()
    suppressed = db.scalar(
        select(SuppressionEntry.id).where(
            SuppressionEntry.workspace_id == workspace_id,
            SuppressionEntry.phone_hash == phone_hash,
            (SuppressionEntry.expires_at.is_(None) | (SuppressionEntry.expires_at > current)),
        )
    )
    if suppressed:
        raise HTTPException(status_code=409, detail="Contact is on the workspace suppression list")
    record = db.scalar(
        select(ConsentRecord)
        .where(
            ConsentRecord.workspace_id == workspace_id,
            ConsentRecord.phone_hash == phone_hash,
            ConsentRecord.channel == channel,
        )
        .order_by(ConsentRecord.captured_at.desc())
        .limit(1)
    )
    if (
        not record
        or record.status != "granted"
        or record.legal_basis != "consent"
        or (record.expires_at is not None and aware(record.expires_at) <= current)
    ):
        label = "recording" if channel == "recording" else "voice-contact"
        raise HTTPException(status_code=409, detail=f"Current explicit {label} consent evidence is required")


def _livekit_service_fields(secret: str, config: dict[str, str], source: str) -> tuple[str, str, str]:
    if source == "platform":
        internal_url = config.get("internal_url", "")
        api_key = config.get("api_key", "")
        api_secret = secret
    else:
        try:
            document = json.loads(secret)
            internal_url = document.get("internalUrl") or document["publicUrl"]
            api_key = document["apiKey"]
            api_secret = document["apiSecret"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=503, detail="Tenant LiveKit credential is invalid") from exc
    origin = exact_livekit_origin(
        internal_url,
        allowed_schemes={"http", "https", "ws", "wss"},
        error_detail="LiveKit service URL is misconfigured",
    )
    http_url = re.sub(r"^wss?://", "https://" if origin.startswith("wss://") else "http://", origin)
    if not api_key or not api_secret:
        raise HTTPException(status_code=503, detail="LiveKit service credential is incomplete")
    return http_url, api_key, api_secret


def _workspace_sip_trunk(settings: Settings, workspace_id: int, provider: str) -> str:
    try:
        mapping = json.loads(settings.OUTBOUND_SIP_TRUNK_MAP_JSON or "{}")
        workspace_mapping = mapping.get(str(workspace_id), {})
        value = workspace_mapping.get(provider, "")
    except (AttributeError, json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="Workspace SIP trunk mapping is invalid") from exc
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,200}", value):
        raise HTTPException(status_code=503, detail="Workspace SIP trunk is not provisioned")
    return value


class StartCallBody(BaseModel):
    agent_id: int = Field(ge=1, alias="agentId")
    to_number: str = Field(min_length=9, max_length=16, alias="toNumber")
    provider: str | None = Field(default=None, min_length=2, max_length=64)


@router.post("/api/calls", status_code=201)
async def start_call(
    body: StartCallBody,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("member", "voice"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    assert access.license is not None
    require_feature(access.license, "telephony")
    to_number = body.to_number.strip()
    if not E164_RE.fullmatch(to_number):
        raise HTTPException(status_code=422, detail="toNumber must use E.164 format")
    agent = db.scalar(
        select(Agent).where(
            Agent.id == body.agent_id,
            Agent.workspace_id == access.workspace.id,
            Agent.status == "published",
        )
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Published agent not found")
    _require_current_consent(db, settings, access.workspace.id, to_number, "voice")
    if agent.recording_enabled:
        require_feature(access.license, "recordings")
        _require_current_consent(db, settings, access.workspace.id, to_number, "recording")

    telephony_source = provider_source(access.license, "telephony")
    carrier = "platform"
    trunk_id = settings.OUTBOUND_SIP_TRUNK_ID.strip() if telephony_source == "platform" else ""
    if telephony_source == "byok":
        query = select(ProviderConnection).where(
            ProviderConnection.workspace_id == access.workspace.id,
            ProviderConnection.kind == "telephony",
            ProviderConnection.status == "active",
        )
        if body.provider:
            query = query.where(ProviderConnection.provider == body.provider.strip().lower())
        connection = db.scalar(query.order_by(ProviderConnection.updated_at.desc()).limit(1))
        if not connection:
            raise HTTPException(status_code=503, detail="Tenant telephony provider is not configured")
        carrier = connection.provider
        trunk_id = _workspace_sip_trunk(
            settings,
            access.workspace.id,
            carrier,
        )
    if not settings.OUTBOUND_SIP_ENABLED or not trunk_id:
        raise HTTPException(status_code=503, detail="Outbound SIP execution is not configured")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,200}", trunk_id):
        raise HTTPException(status_code=503, detail="Outbound SIP trunk is misconfigured")
    if provider_source(access.license, "realtime") != "platform":
        raise HTTPException(
            status_code=403,
            detail="This single-worker deployment requires platform realtime transport",
        )
    realtime = resolve_runtime_credential(db, access.license, "realtime", "livekit", settings)
    livekit_url, api_key, api_secret = _livekit_service_fields(
        realtime.secret, realtime.config, realtime.source
    )
    current = now_utc()
    approved_call_seconds = min(
        agent.max_call_seconds,
        max(0, int((access.license.valid_until - current).total_seconds())),
    )
    if approved_call_seconds < 30:
        raise HTTPException(status_code=402, detail="License expires too soon to start a voice call")
    # Resolve and cost-check the exact LLM/STT/TTS stack before any carrier is
    # dialled. The returned secret-bearing mapping is deliberately discarded.
    agent_snapshot = runtime_agent_snapshot(
        agent,
        access.license,
        max_call_seconds=approved_call_seconds,
    )
    resolve_agent_runtime_providers(db, access.license, agent_snapshot, settings)
    db.execute(
        update(CallSession)
        .where(
            CallSession.workspace_id == access.workspace.id,
            CallSession.status == "queued",
            CallSession.reservation_expires_at.is_not(None),
            CallSession.reservation_expires_at <= current,
        )
        .values(status="expired", reserved_voice_seconds=0, reserved_tokens=0)
    )
    voice_limit = access.license.quotas.get("voice_seconds")
    if voice_limit is not None:
        used = check_quota(db, access.license, "voice_seconds")
        reserved = (
            db.scalar(
                select(func.coalesce(func.sum(CallSession.reserved_voice_seconds), 0)).where(
                    CallSession.license_id == access.license.license.id,
                    CallSession.status == "queued",
                    CallSession.reservation_expires_at > current,
                )
            )
            or 0
        )
        if used + reserved + approved_call_seconds > voice_limit:
            raise HTTPException(status_code=402, detail="License voice_seconds quota cannot cover this call")
    token_reservation = platform_voice_token_reservation(access.license, approved_call_seconds)
    token_limit = access.license.quotas.get("tokens")
    if token_reservation and token_limit is not None:
        used_tokens = check_quota(db, access.license, "tokens")
        reserved_tokens = (
            db.scalar(
                select(func.coalesce(func.sum(CallSession.reserved_tokens), 0)).where(
                    CallSession.license_id == access.license.license.id,
                    CallSession.status == "queued",
                    CallSession.reservation_expires_at > current,
                )
            )
            or 0
        )
        if used_tokens + reserved_tokens + token_reservation > token_limit:
            raise HTTPException(status_code=402, detail="License tokens quota cannot cover this call")

    setup_voice = min(10, approved_call_seconds)
    setup_tokens = min(256, token_reservation)
    room_name = f"call-w{access.workspace.id}-a{agent.id}-{uuid.uuid4().hex}"
    call = CallSession(
        workspace_id=access.workspace.id,
        license_id=access.license.license.id,
        agent_id=agent.id,
        room_name=room_name,
        direction="outbound",
        status="queued",
        to_number=to_number,
        provider=carrier,
        reserved_voice_seconds=approved_call_seconds - setup_voice,
        reserved_tokens=token_reservation - setup_tokens,
        reservation_expires_at=current + timedelta(seconds=approved_call_seconds + 300),
        details={
            "startedByUserId": access.user.id,
            "consentVerified": True,
            "recordingConsentVerified": agent.recording_enabled,
            "platformLlm": token_reservation > 0,
            "approvedVoiceSeconds": approved_call_seconds,
            "approvedTokens": token_reservation,
            "voiceChargedAtStart": setup_voice,
            "tokensChargedAtStart": setup_tokens,
            "agentRuntimeSnapshot": agent_snapshot,
            "postCallPlan": snapshot_post_call_plan(db, agent),
        },
    )
    db.add(call)
    db.flush()
    db.add(
        AuditLog(
            workspace_id=access.workspace.id,
            actor=f"user:{access.user.id}",
            action="call.requested",
            resource_type="call",
            resource_id=str(call.id),
            details={"agentId": agent.id, "provider": carrier},
        )
    )
    # A dial attempt can incur carrier/room cost even if the worker never joins.
    # Charge a small non-refundable setup allowance; the worker precharges only
    # the remaining approved maximum when it receives configuration.
    consume_quota(db, access.license, "voice_seconds", setup_voice)
    if setup_tokens:
        consume_quota(db, access.license, "tokens", setup_tokens)
    db.add(
        UsageEvent(
            workspace_id=access.workspace.id,
            license_id=access.license.license.id,
            kind="voice_seconds",
            quantity=setup_voice,
            provider="outbound-setup",
        )
    )
    if setup_tokens:
        db.add(
            UsageEvent(
                workspace_id=access.workspace.id,
                license_id=access.license.license.id,
                kind="tokens",
                quantity=setup_tokens,
                provider="outbound-setup",
            )
        )
    db.commit()

    livekit_client = None
    try:
        from livekit import api

        livekit_client = api.LiveKitAPI(livekit_url, api_key, api_secret)
        await asyncio.wait_for(
            livekit_client.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(agent_name="saas-agent", room=room_name)
            ),
            timeout=15,
        )
        # Re-authorize under the tenant lock, then commit before the external
        # SIP request. This keeps database locks away from an unbounded carrier
        # round trip. Concurrent revocation cancels the `dialing` row, and the
        # post-flight check below tears down any participant created in the race.
        db.rollback()
        db.scalar(select(Workspace).where(Workspace.id == access.workspace.id).with_for_update())
        final_claims = active_license_for_workspace(db, access.workspace.id, settings, lock=True)
        require_feature(final_claims, "voice")
        require_feature(final_claims, "telephony")
        final_agent = db.scalar(
            select(Agent).where(
                Agent.id == body.agent_id,
                Agent.workspace_id == access.workspace.id,
                Agent.status == "published",
            )
        )
        if not final_agent:
            raise HTTPException(status_code=409, detail="Agent was unpublished before dial")
        current_call = db.scalar(
            select(CallSession)
            .where(
                CallSession.id == call.id,
                CallSession.workspace_id == access.workspace.id,
                CallSession.license_id == final_claims.license.id,
                CallSession.status.in_(["queued", "active"]),
            )
            .with_for_update()
        )
        if not current_call:
            raise HTTPException(status_code=409, detail="Call authorization was revoked before dial")
        _require_current_consent(db, settings, access.workspace.id, to_number, "voice")
        if final_agent.recording_enabled:
            _require_current_consent(db, settings, access.workspace.id, to_number, "recording")
        if provider_source(final_claims, "realtime") != "platform":
            raise HTTPException(status_code=409, detail="Realtime authorization changed before dial")
        resolve_agent_runtime_providers(
            db,
            final_claims,
            call_agent_snapshot(current_call, access.workspace.id, final_agent.id),
            settings,
        )
        if provider_source(final_claims, "telephony") != telephony_source:
            raise HTTPException(status_code=409, detail="Telephony authorization changed before dial")
        if telephony_source == "byok" and not db.scalar(
            select(ProviderConnection.id).where(
                ProviderConnection.workspace_id == access.workspace.id,
                ProviderConnection.kind == "telephony",
                ProviderConnection.provider == carrier,
                ProviderConnection.status == "active",
            )
        ):
            raise HTTPException(status_code=409, detail="Telephony provider was disabled before dial")
        current_call.status = "dialing"
        db.commit()
        participant = await asyncio.wait_for(
            livekit_client.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    sip_trunk_id=trunk_id,
                    sip_call_to=to_number,
                    room_name=room_name,
                    participant_identity=f"callee-{uuid.uuid4().hex[:16]}",
                    participant_name="Outbound caller",
                    wait_until_answered=False,
                )
            ),
            timeout=15,
        )
    except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
        if livekit_client is not None:
            try:
                from livekit import api

                await livekit_client.room.delete_room(api.DeleteRoomRequest(room=room_name))
            except Exception:  # noqa: BLE001,S110
                pass
        db.rollback()
        failed = db.scalar(
            select(CallSession)
            .where(CallSession.id == call.id, CallSession.workspace_id == access.workspace.id)
            .with_for_update()
        )
        if failed:
            if failed.status in {"queued", "dialing", "active"}:
                failed.status = "failed"
                failed.reserved_voice_seconds = 0
                failed.reserved_tokens = 0
                failed.reservation_expires_at = None
                failed.ended_at = now_utc()
                failed.details = {**failed.details, "failureCode": "sip_execution_failed"}
                license_row = db.get(License, failed.license_id)
                if license_row is not None:
                    claims = verified_claims(
                        license_row,
                        settings,
                        require_active=False,
                        enforce_dates=False,
                    )
                    charged_voice = int(failed.details.get("voiceChargedAtStart") or 0)
                    charged_tokens = int(failed.details.get("tokensChargedAtStart") or 0)
                    if charged_voice:
                        adjust_consumed_quota(db, claims, "voice_seconds", -charged_voice)
                        db.add(
                            UsageEvent(
                                workspace_id=failed.workspace_id,
                                license_id=failed.license_id,
                                kind="voice_seconds",
                                quantity=-charged_voice,
                                provider="outbound-refund",
                            )
                        )
                    if charged_tokens:
                        adjust_consumed_quota(db, claims, "tokens", -charged_tokens)
                        db.add(
                            UsageEvent(
                                workspace_id=failed.workspace_id,
                                license_id=failed.license_id,
                                kind="tokens",
                                quantity=-charged_tokens,
                                provider="outbound-refund",
                            )
                        )
                db.commit()
            else:
                db.rollback()
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(status_code=502, detail="Outbound call could not be started") from exc
    finally:
        if livekit_client is not None:
            try:
                await livekit_client.aclose()
            except Exception:  # noqa: BLE001,S110
                pass

    db.rollback()
    db.scalar(select(Workspace).where(Workspace.id == access.workspace.id).with_for_update())
    current_call = db.scalar(
        select(CallSession)
        .where(CallSession.id == call.id, CallSession.workspace_id == access.workspace.id)
        .with_for_update()
    )
    if not current_call:
        raise HTTPException(status_code=500, detail="Call record is unavailable")
    if current_call.status == "canceled":
        enqueue_room_termination(db, current_call, "startup_authorization_revoked")
        db.commit()
        raise HTTPException(status_code=409, detail="Call was revoked during startup")
    final_claims = active_license_for_workspace(db, access.workspace.id, settings, lock=True)
    if final_claims.license.id != current_call.license_id:
        current_call.status = "canceled"
        current_call.ended_at = now_utc()
        enqueue_room_termination(db, current_call, "startup_license_changed")
        db.commit()
        raise HTTPException(status_code=409, detail="Call authorization changed during startup")
    current_call.status = "active"
    current_call.details = {
        **current_call.details,
        "sipParticipantId": getattr(participant, "participant_id", ""),
        "sipCallId": getattr(participant, "sip_call_id", ""),
    }
    db.commit()
    return {"call": call_json(current_call)}


@router.get("/api/live-occupancy")
def occupancy(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer", "analytics"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    rows = db.execute(
        select(CallSession, Agent)
        .join(Agent, Agent.id == CallSession.agent_id)
        .where(
            CallSession.workspace_id == access.workspace.id,
            CallSession.status == "active",
            CallSession.reservation_expires_at > now_utc(),
        )
    ).all()
    sessions = [
        {
            **call_json(call),
            "agentName": agent.name,
        }
        for call, agent in rows
    ]
    by_agent = Counter((call.agent_id, agent.name) for call, agent in rows)
    by_provider = Counter(call.provider for call, _agent in rows)
    return {
        "live": len(rows),
        "onAir": len(rows),
        "byAgent": [
            {"agentId": agent_id, "agentName": name, "live": count}
            for (agent_id, name), count in by_agent.items()
        ],
        "byProvider": [{"provider": provider, "live": count} for provider, count in by_provider.items()],
        "sessions": sessions,
        "calls": sessions,
    }


@router.get("/api/analytics")
def analytics(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer", "analytics"))],
    db: Annotated[Session, Depends(get_db)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, object]:
    since = now_utc() - timedelta(days=days)
    rows = db.scalars(
        select(CallSession)
        .where(CallSession.workspace_id == access.workspace.id, CallSession.created_at >= since)
        .order_by(CallSession.created_at.desc())
        .limit(10_000)
    ).all()
    sentiments = Counter(row.sentiment or "unknown" for row in rows)
    dispositions = Counter(row.disposition or "unassigned" for row in rows)
    statuses = Counter(row.status for row in rows)
    directions = Counter(row.direction for row in rows)
    completed = sum(1 for row in rows if row.pipeline_completed)
    transfers = sum(row.transfer_count for row in rows)
    transferred_calls = sum(row.transfer_count > 0 for row in rows)
    usage = db.execute(
        select(UsageEvent.provider, func.coalesce(func.sum(UsageEvent.quantity), 0))
        .where(UsageEvent.workspace_id == access.workspace.id, UsageEvent.created_at >= since)
        .group_by(UsageEvent.provider)
    ).all()
    totals = {
        "calls": len(rows),
        "durationSeconds": sum(row.duration_seconds for row in rows),
        "costMicros": sum(row.cost_micros for row in rows),
        "creditsUsed": sum(row.credits_used for row in rows),
        "pipelineCompleted": completed,
        "pipelineCompletionRate": round((completed / len(rows)) * 100, 2) if rows else 0,
        "transfers": transfers,
        "transferredCalls": transferred_calls,
        "transferRate": round((transferred_calls / len(rows)) * 100, 2) if rows else 0,
    }
    return {
        "windowDays": days,
        "totals": totals,
        "statuses": _counter_rows(statuses),
        "sentiments": _counter_rows(sentiments),
        "dispositions": _counter_rows(dispositions),
        "directions": _counter_rows(directions),
        "providers": [
            {"label": provider or "unknown", "quantity": int(quantity)} for provider, quantity in usage
        ],
        "recent": [call_json(row) for row in rows[:50]],
        "stats": stats(db, access),
        "sentimentCounts": dict(sentiments),
        "dispositionCounts": dict(dispositions),
        "pipelineCompletionRate": totals["pipelineCompletionRate"],
        "credits": totals["creditsUsed"],
    }


def _counter_rows(values: Counter[str]) -> list[dict[str, object]]:
    return [{"label": label, "value": count} for label, count in values.most_common()]


@router.get("/api/reports/daily", response_model=None)
def daily_report(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer", "analytics"))],
    db: Annotated[Session, Depends(get_db)],
    date: Date | None = None,
    timezone: str = "UTC",
    agent_id: Annotated[int | None, Query(alias="agentId", ge=1)] = None,
    format: Literal["json", "csv"] = "json",
) -> dict[str, object] | Response:
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="timezone is invalid") from exc
    report_date = date or datetime.now(zone).date()
    since = datetime.combine(report_date, time.min, zone).astimezone(UTC)
    until = since + timedelta(days=1)
    query = select(CallSession).where(
        CallSession.workspace_id == access.workspace.id,
        CallSession.created_at >= since,
        CallSession.created_at < until,
    )
    if agent_id is not None:
        query = query.where(CallSession.agent_id == agent_id)
    rows = db.scalars(query.order_by(CallSession.created_at.asc())).all()
    buckets = {"0–30 sec": 0, "31–60 sec": 0, "1–3 min": 0, "3+ min": 0}
    for row in rows:
        seconds = row.duration_seconds
        key = (
            "0–30 sec"
            if seconds <= 30
            else "31–60 sec"
            if seconds <= 60
            else "1–3 min"
            if seconds <= 180
            else "3+ min"
        )
        buckets[key] += 1
    completed = sum(row.pipeline_completed for row in rows)
    transferred = sum(row.transfer_count > 0 for row in rows)
    dispositions = Counter(row.disposition or "unassigned" for row in rows)
    totals = {
        "calls": len(rows),
        "completed": completed,
        "completionRate": round(completed / len(rows) * 100, 2) if rows else 0,
        "transfers": sum(row.transfer_count for row in rows),
        "transferRate": round(transferred / len(rows) * 100, 2) if rows else 0,
        "durationSeconds": sum(row.duration_seconds for row in rows),
        "creditsUsed": sum(row.credits_used for row in rows),
        "costMicros": sum(row.cost_micros for row in rows),
    }
    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "call_id",
                "agent_id",
                "status",
                "duration_seconds",
                "disposition",
                "sentiment",
                "transfers",
                "credits",
                "created_at",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.id,
                    row.agent_id,
                    row.status,
                    row.duration_seconds,
                    row.disposition or "",
                    row.sentiment or "",
                    row.transfer_count,
                    row.credits_used,
                    row.created_at.isoformat(),
                ]
            )
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="calls-{report_date.isoformat()}.csv"'},
        )
    return {
        "date": report_date.isoformat(),
        "timezone": timezone,
        "agentId": agent_id,
        "totals": totals,
        "durationBuckets": [{"label": label, "value": value} for label, value in buckets.items()],
        "dispositions": _counter_rows(dispositions),
    }


@router.get("/api/telephony")
def telephony(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer", "telephony"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    connections = db.scalars(
        select(ProviderConnection).where(
            ProviderConnection.workspace_id == access.workspace.id,
            ProviderConnection.kind == "telephony",
        )
    ).all()
    return {
        "providers": ["twilio", "vonage", "vobiz", "convox", "telnyx", "cloudonix", "asterisk"],
        "connections": [
            {
                "id": row.id,
                "provider": row.provider,
                "label": row.label,
                "status": row.status,
                "config": safe_public_provider_config(row),
                "updatedAt": row.updated_at.isoformat(),
            }
            for row in connections
        ],
    }


class PhoneBody(BaseModel):
    provider: str = Field(pattern="^(twilio|vonage|vobiz|convox|telnyx|cloudonix|asterisk)$")
    e164: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    label: str = Field(default="Main line", min_length=1, max_length=100)
    direction: str = Field(default="both", pattern="^(inbound|outbound|both)$")
    provider_ref: str = Field(default="", max_length=200, alias="providerRef")


@router.get("/api/phone-numbers")
def phone_numbers(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer", "telephony"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    rows = db.scalars(
        select(PhoneNumber)
        .where(PhoneNumber.workspace_id == access.workspace.id)
        .order_by(PhoneNumber.id.desc())
    ).all()
    numbers = [
        {
            "id": row.id,
            "provider": row.provider,
            "e164": row.e164,
            "label": row.label,
            "direction": row.direction,
            "status": row.status,
            "createdAt": row.created_at.isoformat(),
        }
        for row in rows
    ]
    return {
        "numbers": numbers,
        "phoneNumbers": numbers,
    }


@router.post("/api/phone-numbers", status_code=201)
def save_phone_number(
    body: PhoneBody,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("admin", "telephony"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    connection = db.scalar(
        select(ProviderConnection.id).where(
            ProviderConnection.workspace_id == access.workspace.id,
            ProviderConnection.kind == "telephony",
            ProviderConnection.provider == body.provider,
            ProviderConnection.status == "active",
        )
    )
    if not connection:
        raise HTTPException(status_code=422, detail="Configure this tenant telephony provider first")
    number = PhoneNumber(
        workspace_id=access.workspace.id,
        provider=body.provider,
        provider_ref=body.provider_ref,
        e164=body.e164,
        label=body.label,
        direction=body.direction,
    )
    db.add(number)
    db.commit()
    payload = {
        "id": number.id,
        "provider": number.provider,
        "e164": number.e164,
        "label": number.label,
        "direction": number.direction,
        "status": number.status,
    }
    return {"number": payload, "phoneNumber": payload}


@router.api_route("/api/campaigns", methods=["GET", "POST", "PATCH", "DELETE"])
@router.api_route("/api/campaigns/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
def campaigns_phase_two(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("operator"))],
    path: str = "",
) -> None:
    raise HTTPException(
        status_code=501, detail="Campaign dispatch is a phase-2 service; endpoint is reserved"
    )
