from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..dependencies import WorkspaceAccess, require_workspace
from ..licensing import (
    LicenseClaims,
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
from ..models import Agent, AuditLog, CallSession, License, ProviderConnection, StoredFile, UsageEvent, Workspace
from ..provider_runtime import expand_llm_provider_order
from ..provider_vault import (
    RuntimeCredential,
    SUPPORTED_PROVIDERS,
    enforce_platform_model,
    resolve_groq_model,
    resolve_runtime_credential,
)
from ..postcall import (
    post_call_stats,
    prepare_post_call_jobs,
    run_post_call_background,
    snapshot_post_call_plan,
)
from ..security import authenticate_worker, now_utc
from ..storage import resolve_storage_key
from ..worker_envelope import seal_runtime_providers

router = APIRouter(tags=["voice"])
ROOM_RE = re.compile(r"^(test|call)-w([1-9]\d{0,9})-a([1-9]\d{0,9})-[A-Za-z0-9_-]{8,80}$")


def runtime_agent_snapshot(
    agent: Agent,
    claims: LicenseClaims,
    *,
    max_call_seconds: int | None = None,
) -> dict[str, Any]:
    """Freeze the auditable non-secret agent definition before a room is issued."""

    return {
        "id": agent.id,
        "workspaceId": agent.workspace_id,
        "name": agent.name,
        "objective": agent.objective,
        "globalPrompt": agent.global_prompt,
        "greeting": agent.greeting,
        "locale": agent.locale,
        "voice": agent.voice,
        "model": agent.model,
        "maxCallSeconds": max_call_seconds or agent.max_call_seconds,
        "recordingEnabled": bool(agent.recording_enabled and "recordings" in claims.features),
        "workflow": agent.workflow,
        "humanHandoffNumber": agent.human_handoff_number,
        "providerPolicy": agent.provider_policy,
    }


class LiveKitTokenBody(BaseModel):
    agent_id: int = Field(ge=1, alias="agentId")
    session_id: str | None = Field(default=None, min_length=8, max_length=64, alias="sessionId")


def _livekit_fields(credential: RuntimeCredential, settings: Settings) -> tuple[str, str, str]:
    if credential.source == "platform":
        return (
            credential.config.get("public_url", ""),
            credential.config.get("api_key", ""),
            credential.secret,
        )
    # LiveKit has three credentials. For BYOK they are kept together inside the
    # encrypted secret document, so apiKey never appears in a browser-facing list.
    try:
        value = json.loads(credential.secret)
        public_url = value["publicUrl"]
        api_key = value["apiKey"]
        api_secret = value["apiSecret"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Tenant LiveKit credential must be JSON with publicUrl, apiKey, and apiSecret",
        ) from exc
    if not all(isinstance(item, str) and item for item in (public_url, api_key, api_secret)):
        raise HTTPException(status_code=503, detail="Tenant LiveKit credential is invalid")
    return public_url, api_key, api_secret


def _assert_public_livekit_url(value: str, settings: Settings) -> str:
    allowed_schemes = {"wss"} if settings.ENVIRONMENT == "production" else {"ws", "wss"}
    return exact_livekit_origin(
        value,
        allowed_schemes=allowed_schemes,
        error_detail="LiveKit public URL is misconfigured",
    )


@router.post("/api/livekit/token")
def livekit_token(
    body: LiveKitTokenBody,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("member", "voice"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    agent = db.scalar(
        select(Agent).where(
            Agent.id == body.agent_id,
            Agent.workspace_id == access.workspace.id,
            Agent.status != "archived",
        )
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    assert access.license is not None
    if provider_source(access.license, "realtime") != "platform":
        raise HTTPException(
            status_code=403,
            detail="This single-worker deployment requires platform realtime transport for voice",
        )
    credential = resolve_runtime_credential(db, access.license, "realtime", "livekit", settings)
    public_url, api_key, api_secret = _livekit_fields(credential, settings)
    public_url = _assert_public_livekit_url(public_url, settings)

    # Serialise starts for this tenant and include active reservations when
    # enforcing the signed voice-seconds entitlement.
    db.scalar(select(Workspace).where(Workspace.id == access.workspace.id).with_for_update())
    current = now_utc()
    approved_call_seconds = min(
        agent.max_call_seconds,
        max(0, int((access.license.valid_until - current).total_seconds())),
    )
    if approved_call_seconds < 30:
        raise HTTPException(status_code=402, detail="License expires too soon to start a voice session")
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
    limit = access.license.quotas.get("voice_seconds")
    if limit is not None:
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
        if used + reserved + approved_call_seconds > limit:
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

    requested_session = body.session_id or uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", requested_session):
        raise HTTPException(status_code=422, detail="sessionId is invalid")
    namespace = hashlib.sha256(f"{access.workspace.id}|{access.user.id}".encode()).hexdigest()[:12]
    room_name = f"test-w{access.workspace.id}-a{agent.id}-{namespace}-{requested_session}"[:110]
    if db.scalar(select(CallSession.id).where(CallSession.room_name == room_name)):
        raise HTTPException(status_code=409, detail="This voice session identifier is already in use")
    participant_id = f"u{access.user.id}-{uuid.uuid4().hex[:16]}"
    try:
        from livekit import api

        token_builder = (
            api.AccessToken(api_key, api_secret)
            .with_identity(participant_id)
            .with_ttl(timedelta(minutes=5))
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=False,
                    can_publish_sources=["microphone"],
                    can_update_own_metadata=False,
                )
            )
            .with_room_config(
                api.RoomConfiguration(
                    max_participants=2,
                    agents=[api.RoomAgentDispatch(agent_name="saas-agent")],
                )
            )
        )
        participant_token = token_builder.to_jwt()
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="LiveKit token service is misconfigured") from exc

    agent_snapshot = runtime_agent_snapshot(
        agent,
        access.license,
        max_call_seconds=approved_call_seconds,
    )
    call = CallSession(
        workspace_id=access.workspace.id,
        license_id=access.license.license.id,
        agent_id=agent.id,
        room_name=room_name,
        direction="test",
        status="queued",
        reserved_voice_seconds=approved_call_seconds,
        reserved_tokens=token_reservation,
        reservation_expires_at=current + timedelta(minutes=5),
        details={
            "startedByUserId": access.user.id,
            "platformLlm": token_reservation > 0,
            "approvedVoiceSeconds": approved_call_seconds,
            "approvedTokens": token_reservation,
            "agentRuntimeSnapshot": agent_snapshot,
            "postCallPlan": snapshot_post_call_plan(db, agent),
        },
    )
    db.add(call)
    db.add(
        AuditLog(
            workspace_id=access.workspace.id,
            actor=f"user:{access.user.id}",
            action="voice.test_started",
            resource_type="agent",
            resource_id=str(agent.id),
            details={"room": room_name},
        )
    )
    db.commit()
    runtime = resolve_agent_runtime_providers(db, access.license, agent_snapshot, settings)
    fallback = [kind for kind in ("llm", "stt", "tts") if kind in (runtime.get("inferenceFallback") or [])]
    payload: dict[str, object] = {
        "server_url": public_url,
        "participant_token": participant_token,
        "room_name": room_name,
    }
    if fallback:
        payload["voice_notice"] = voice_inference_notice(fallback)
        payload["inference_fallback"] = fallback
    return payload


class RoomBody(BaseModel):
    room_name: str = Field(min_length=20, max_length=120, alias="roomName")


def _room_parts(room_name: str) -> tuple[int, int]:
    match = ROOM_RE.fullmatch(room_name)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid room namespace")
    return int(match.group(2)), int(match.group(3))


def _runtime_descriptor(
    credential: RuntimeCredential,
    kind: str,
    agent_snapshot: dict[str, Any],
) -> dict[str, Any]:
    defaults = {
        ("llm", "openai"): "gpt-4.1-mini",
        ("llm", "groq"): "openai/gpt-oss-120b",
        ("llm", "anthropic"): "claude-3-5-haiku-latest",
        ("stt", "elevenlabs"): "scribe_v2_realtime",
        ("stt", "openai"): "gpt-4o-mini-transcribe",
        ("stt", "deepgram"): "nova-3",
        ("tts", "elevenlabs"): "eleven_flash_v2_5",
        ("tts", "openai"): "gpt-4o-mini-tts",
        ("tts", "deepgram"): "aura-2-andromeda-en",
    }
    configured_model = credential.config.get("model", "")
    agent_model = str(agent_snapshot.get("model") or "")
    if kind == "llm" and (
        (credential.provider == "openai" and agent_model.startswith("gpt-"))
        or (credential.provider == "groq" and agent_model.startswith(("llama", "mixtral", "gemma", "openai/", "qwen/")))
        or (credential.provider == "anthropic" and agent_model.startswith("claude-"))
    ):
        configured_model = agent_model
    selected_model = configured_model or defaults.get((kind, credential.provider), "")
    if kind == "llm" and credential.provider == "groq":
        selected_model = resolve_groq_model(selected_model, credential.config.get("model", ""))
    selected_model = enforce_platform_model(kind, credential.provider, selected_model, credential.source)
    result: dict[str, Any] = {
        "provider": credential.provider,
        "apiKey": credential.secret,
        "credentialSource": credential.source,
        "model": selected_model,
    }
    if kind == "stt":
        result["languageCode"] = credential.config.get("languageCode", "")
    elif kind == "tts":
        if credential.provider == "elevenlabs":
            result["voice"] = credential.config.get("voiceId", "21m00Tcm4TlvDq8ikWAM")
        elif credential.provider == "deepgram":
            voice = credential.config.get("voice", "")
            if voice:
                result["voice"] = voice
        else:
            result["voice"] = credential.config.get("voice", "ash")
    return result


LIVEKIT_INFERENCE_MODELS = {
    "stt": {"model": "elevenlabs/scribe_v2_realtime"},
    "llm": {"model": "openai/gpt-4.1-mini"},
    "tts": {"model": "elevenlabs/eleven_flash_v2_5", "voice": "Rachel"},
}


def voice_inference_notice(missing: list[str]) -> str:
    labels = {"llm": "LLM", "stt": "speech-to-text (STT)", "tts": "text-to-speech (TTS)"}
    named = [labels[kind] for kind in missing if kind in labels]
    joined = " and ".join(named) if len(named) <= 2 else f"{', '.join(named[:-1])}, and {named[-1]}"
    verb = "is" if len(named) == 1 else "are"
    return (
        f"Tenant {joined} {verb} not configured. This voice session uses LiveKit Inference. "
        "Add Deepgram, ElevenLabs, or OpenAI keys in Settings → Providers to use your own STT/TTS."
    )


def _inference_descriptor(kind: str) -> dict[str, Any]:
    return {
        "provider": "livekit",
        "credentialSource": "platform",
        "inference": True,
        **LIVEKIT_INFERENCE_MODELS[kind],
    }


def _provider_order_for_kind(
    db: Session,
    claims: LicenseClaims,
    kind: str,
    provider_order: list[object],
) -> list[str]:
    ordered = [provider for provider in provider_order if isinstance(provider, str)]
    if kind == "llm":
        return expand_llm_provider_order(db, claims, ordered)
    allowed = SUPPORTED_PROVIDERS.get(kind, set())
    result = [provider for provider in ordered if provider in allowed]
    if provider_source(claims, kind) != "byok":
        return result
    extras = db.scalars(
        select(ProviderConnection.provider).where(
            ProviderConnection.workspace_id == claims.license.workspace_id,
            ProviderConnection.kind == kind,
            ProviderConnection.status == "active",
        )
    )
    for provider in extras:
        if provider in allowed and provider not in result:
            result.append(provider)
    return result


def resolve_agent_runtime_providers(
    db: Session,
    claims: LicenseClaims,
    agent_snapshot: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    runtime_providers: dict[str, Any] = {
        "transport": "direct",
        "mode": claims.provider_mode,
        "hybridPolicy": claims.hybrid_policy,
    }
    policy = agent_snapshot.get("providerPolicy")
    if not isinstance(policy, dict):
        raise HTTPException(status_code=409, detail="Call agent snapshot is invalid")
    missing: list[str] = []
    for kind in ("llm", "stt", "tts"):
        configured = False
        provider_order = policy.get(kind, [])
        if not isinstance(provider_order, list):
            raise HTTPException(status_code=409, detail="Call provider snapshot is invalid")
        for provider in _provider_order_for_kind(db, claims, kind, provider_order):
            try:
                credential = resolve_runtime_credential(db, claims, kind, provider, settings)
            except HTTPException as exc:
                if exc.status_code == 503:
                    continue
                raise
            runtime_providers[kind] = _runtime_descriptor(credential, kind, agent_snapshot)
            configured = True
            break
        if not configured:
            missing.append(kind)
    if missing:
        if provider_source(claims, "realtime") != "platform":
            raise HTTPException(status_code=503, detail=f"No configured {missing[0]} provider is available")
        for kind in ("llm", "stt", "tts"):
            runtime_providers[kind] = _inference_descriptor(kind)
        runtime_providers["transport"] = "livekit_inference"
        runtime_providers["inferenceFallback"] = missing
    return runtime_providers


def call_agent_snapshot(call: CallSession, workspace_id: int, agent_id: int) -> dict[str, Any]:
    snapshot = call.details.get("agentRuntimeSnapshot")
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("workspaceId") != workspace_id
        or snapshot.get("id") != agent_id
        or not isinstance(snapshot.get("workflow"), dict)
        or not isinstance(snapshot.get("providerPolicy"), dict)
        or not isinstance(snapshot.get("maxCallSeconds"), int)
        or isinstance(snapshot.get("maxCallSeconds"), bool)
        or not 30 <= snapshot["maxCallSeconds"] <= 14_400
        or not isinstance(snapshot.get("recordingEnabled"), bool)
    ):
        raise HTTPException(status_code=409, detail="Call agent snapshot is unavailable")
    return snapshot


@router.post("/api/internal/agents/config")
def internal_agent_config(
    body: RoomBody,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    authenticate_worker(request, settings)
    workspace_id, agent_id = _room_parts(body.room_name)
    db.scalar(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
    claims = active_license_for_workspace(db, workspace_id, settings, lock=True)
    require_feature(claims, "voice")
    call = db.scalar(
        select(CallSession)
        .where(
            CallSession.room_name == body.room_name,
            CallSession.workspace_id == workspace_id,
            CallSession.agent_id == agent_id,
            CallSession.status.in_(["queued", "dialing", "active"]),
            CallSession.reservation_expires_at > now_utc(),
        )
        .with_for_update()
    )
    if not call:
        raise HTTPException(status_code=404, detail="Call session not found")
    if claims.license.id != call.license_id:
        raise HTTPException(status_code=402, detail="Call license is no longer active")
    agent_snapshot = call_agent_snapshot(call, workspace_id, agent_id)
    # LiveKit transport credentials stay in the worker/container environment;
    # the room-scoped envelope carries only the direct inference providers.
    runtime_providers = resolve_agent_runtime_providers(db, claims, agent_snapshot, settings)
    envelope = seal_runtime_providers(body.room_name, runtime_providers, settings)
    if call.status in {"queued", "dialing"}:
        started = now_utc()
        already_voice = int(call.details.get("voiceChargedAtStart") or 0)
        already_tokens = int(call.details.get("tokensChargedAtStart") or 0)
        approved_voice = int(
            call.details.get("approvedVoiceSeconds") or call.reserved_voice_seconds + already_voice
        )
        voice_precharge = max(0, approved_voice - already_voice)
        token_total = (
            int(call.details.get("approvedTokens") or call.reserved_tokens + already_tokens)
            if call.details.get("platformLlm") is True
            else 0
        )
        token_precharge = max(0, token_total - already_tokens)
        consume_quota(db, claims, "voice_seconds", voice_precharge)
        if token_precharge:
            consume_quota(db, claims, "tokens", token_precharge)
        call.status = "active"
        call.started_at = started
        call.reservation_expires_at = started + timedelta(seconds=int(agent_snapshot["maxCallSeconds"]) + 300)
        call.details = {
            **call.details,
            "voiceChargedAtStart": already_voice + voice_precharge,
            "tokensChargedAtStart": already_tokens + token_precharge,
        }
        db.add(
            UsageEvent(
                workspace_id=workspace_id,
                license_id=claims.license.id,
                kind="voice_seconds",
                quantity=voice_precharge,
                provider="livekit",
            )
        )
        if token_precharge:
            db.add(
                UsageEvent(
                    workspace_id=workspace_id,
                    license_id=claims.license.id,
                    kind="tokens",
                    quantity=token_precharge,
                    provider="voice-llm",
                )
            )
        db.commit()
    return {
        "agent": {
            **agent_snapshot,
            "call": {
                "direction": call.direction,
                "consentVerified": call.details.get("consentVerified") is True,
                "recordingEnabled": agent_snapshot["recordingEnabled"],
                "recordingConsentVerified": call.details.get("recordingConsentVerified") is True,
            },
            "runtimePolicy": {
                "toolAllowlist": [],
                "webhookAllowlist": [],
                "blockedTopics": [],
                # Zero means tenant-funded BYOK usage. A positive value is the
                # exact platform-funded budget precharged for this call and is
                # enforced again by the Python worker before every LLM turn.
                "approvedTokens": int(call.details.get("approvedTokens") or 0),
            },
        },
        "runtimeProvidersEnvelope": envelope,
    }


class RecordingContentBody(RoomBody):
    recording_id: int = Field(ge=1, alias="recordingId")


@router.post("/api/internal/recordings/content")
def internal_recording_content(
    body: RecordingContentBody,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    authenticate_worker(request, settings)
    workspace_id, agent_id = _room_parts(body.room_name)
    claims = active_license_for_workspace(db, workspace_id, settings)
    require_feature(claims, "recordings")
    call = db.scalar(
        select(CallSession).where(
            CallSession.room_name == body.room_name,
            CallSession.workspace_id == workspace_id,
            CallSession.agent_id == agent_id,
            CallSession.status.in_(["queued", "dialing", "active"]),
            CallSession.reservation_expires_at > now_utc(),
        )
    )
    if not call:
        raise HTTPException(status_code=404, detail="Active call session not found")
    if call.license_id != claims.license.id:
        raise HTTPException(status_code=402, detail="Call license is no longer active")
    snapshot = call_agent_snapshot(call, workspace_id, agent_id)
    workflow = snapshot["workflow"]
    nodes = workflow.get("nodes", []) if isinstance(workflow.get("nodes"), list) else []
    allowed_ids = {
        node.get("config", {}).get("audioRecordingId")
        for node in nodes
        if isinstance(node, dict) and node.get("type") == "Audio" and isinstance(node.get("config"), dict)
    }
    if body.recording_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Recording is not available to this agent")
    record = db.scalar(
        select(StoredFile).where(
            StoredFile.id == body.recording_id,
            StoredFile.workspace_id == workspace_id,
            StoredFile.category == "recordings",
            StoredFile.status == "ready",
        )
    )
    if (
        not record
        or record.size > 5 * 1024 * 1024
        or record.content_type not in {"audio/wav", "audio/x-wav"}
        or record.details.get("safetyStatus") != "approved"
        or not record.storage_key.startswith(f"recordings/w{workspace_id}/")
    ):
        raise HTTPException(status_code=404, detail="Recording is unavailable")
    path = resolve_storage_key(settings, record.storage_key)
    return FileResponse(
        path,
        media_type=record.content_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


class CompleteCallBody(RoomBody):
    transcript: str = Field(default="", max_length=200_000)
    summary: str = Field(default="", max_length=4_000)
    sentiment: str | None = Field(default=None, pattern="^(positive|negative|neutral|mixed)$")
    disposition: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_-]{1,31}$")
    pipeline_completed: bool = Field(default=False, alias="pipelineCompleted")
    transfer_count: int = Field(default=0, ge=0, le=100, alias="transferCount")
    credits_used: int = Field(default=0, ge=0, le=1_000_000_000, alias="creditsUsed")
    duration_seconds: int = Field(ge=0, le=14_400, alias="durationSeconds")
    recording_key: str | None = Field(default=None, max_length=500, alias="recordingKey")
    cost_micros: int = Field(default=0, ge=0, le=1_000_000_000, alias="costMicros")
    tokens_used: int | None = Field(default=None, ge=0, le=50_000_000, alias="tokensUsed")
    safety_events: list[str] = Field(default_factory=list, max_length=20, alias="safetyEvents")

    @field_validator("safety_events")
    @classmethod
    def validate_safety_events(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", item) for item in value):
            raise ValueError("safetyEvents must contain safe event codes")
        return value


def _lock_call_completion_records(
    db: Session,
    *,
    workspace_id: int,
    agent_id: int,
    room_name: str,
) -> tuple[CallSession, License]:
    """Lock Workspace -> License -> CallSession, matching every entitlement mutation."""

    workspace = db.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update(of=Workspace))
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    license_row = db.scalar(
        select(License)
        .join(CallSession, CallSession.license_id == License.id)
        .where(
            CallSession.room_name == room_name,
            CallSession.workspace_id == workspace_id,
            CallSession.agent_id == agent_id,
            License.workspace_id == workspace_id,
        )
        .with_for_update(of=License)
    )
    if not license_row:
        raise HTTPException(status_code=404, detail="Call not found")
    call = db.scalar(
        select(CallSession)
        .where(
            CallSession.room_name == room_name,
            CallSession.workspace_id == workspace_id,
            CallSession.agent_id == agent_id,
            CallSession.license_id == license_row.id,
        )
        .with_for_update(of=CallSession)
    )
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return call, license_row


@router.post("/api/internal/calls/complete")
def internal_call_complete(
    body: CompleteCallBody,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    authenticate_worker(request, settings)
    workspace_id, agent_id = _room_parts(body.room_name)
    call, license_row = _lock_call_completion_records(
        db,
        workspace_id=workspace_id,
        agent_id=agent_id,
        room_name=body.room_name,
    )
    if call.status == "completed":
        prepare_post_call_jobs(db, call)
        db.commit()
        post_call = post_call_stats(db, call.id, call.workspace_id)
        if post_call["pending"]:
            background_tasks.add_task(run_post_call_background, call.id, settings)
        return {
            "completed": True,
            "callId": call.id,
            "idempotent": True,
            "postCall": post_call,
        }
    if call.status == "canceled" and isinstance(call.details.get("lateCompletion"), dict):
        return {
            "completed": True,
            "canceled": True,
            "callId": call.id,
            "idempotent": True,
            "postCall": {"evaluations": 0, "webhooks": 0, "failures": 0, "pending": 0},
        }
    if call.status not in {"queued", "dialing", "active", "canceled"}:
        raise HTTPException(status_code=409, detail="Call is not eligible for completion")
    canceled_completion = call.status == "canceled"
    voice_precharged = int(call.details.get("voiceChargedAtStart") or 0)
    approved_voice_seconds = int(call.details.get("approvedVoiceSeconds") or call.reserved_voice_seconds)
    reconcilable_voice_seconds = voice_precharged if canceled_completion else approved_voice_seconds
    if body.duration_seconds > reconcilable_voice_seconds:
        raise HTTPException(status_code=422, detail="Call duration exceeds its approved maximum")
    if body.recording_key:
        expected_prefix = f"calls/w{workspace_id}/"
        suffix = body.recording_key.removeprefix(expected_prefix)
        if (
            not body.recording_key.startswith(expected_prefix)
            or not suffix
            or "/" in suffix
            or "\\" in suffix
            or suffix in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.(?:wav|mp3|ogg|webm)", suffix, re.I)
        ):
            raise HTTPException(status_code=422, detail="Recording key is outside the workspace namespace")
    # Completion must remain recordable if the license expires while the call is
    # active; signed claims and the originally reserved maximum remain authoritative.
    claims = verified_claims(license_row, settings, require_active=False, enforce_dates=False)
    voice_adjustment = body.duration_seconds - voice_precharged
    charged_tokens = 0
    token_adjustment = 0
    if call.details.get("platformLlm") is True:
        token_precharged = int(call.details.get("tokensChargedAtStart") or 0)
        charged_tokens = body.tokens_used if body.tokens_used is not None else token_precharged
        approved_tokens = int(call.details.get("approvedTokens") or token_precharged)
        reconcilable_tokens = token_precharged if canceled_completion else approved_tokens
        if charged_tokens > reconcilable_tokens:
            raise HTTPException(status_code=422, detail="Call token usage exceeds its approved maximum")
        token_adjustment = charged_tokens - token_precharged
    adjust_consumed_quota(db, claims, "voice_seconds", voice_adjustment)
    if call.details.get("platformLlm") is True:
        adjust_consumed_quota(db, claims, "tokens", token_adjustment)
    if voice_adjustment:
        db.add(
            UsageEvent(
                workspace_id=workspace_id,
                license_id=license_row.id,
                kind="voice_seconds",
                quantity=voice_adjustment,
                provider="livekit",
            )
        )
    if token_adjustment:
        db.add(
            UsageEvent(
                workspace_id=workspace_id,
                license_id=license_row.id,
                kind="tokens",
                quantity=token_adjustment,
                provider="voice-llm",
            )
        )
    if canceled_completion:
        reported_at = now_utc()
        call.duration_seconds = body.duration_seconds
        call.transfer_count = body.transfer_count
        call.credits_used = body.credits_used
        call.cost_micros = body.cost_micros
        call.reserved_voice_seconds = 0
        call.reserved_tokens = 0
        call.reservation_expires_at = None
        call.details = {
            **call.details,
            "safetyEvents": list(body.safety_events),
            "tokensUsed": charged_tokens,
            "lateCompletion": {
                "receivedAt": reported_at.isoformat(),
                "durationSeconds": body.duration_seconds,
                "tokensUsed": charged_tokens,
                "creditsUsed": body.credits_used,
                "costMicros": body.cost_micros,
            },
        }
        db.add(
            AuditLog(
                workspace_id=workspace_id,
                actor="service:voice-worker",
                action="call.canceled_usage_reconciled",
                resource_type="call",
                resource_id=str(call.id),
                details={
                    "durationSeconds": body.duration_seconds,
                    "transferCount": body.transfer_count,
                    "safetyEvents": list(body.safety_events),
                    "tokensUsed": charged_tokens,
                },
            )
        )
        db.commit()
        return {
            "completed": True,
            "canceled": True,
            "callId": call.id,
            "idempotent": False,
            "postCall": {"evaluations": 0, "webhooks": 0, "failures": 0, "pending": 0},
        }
    call.status = "completed"
    call.transcript = body.transcript
    call.summary = body.summary
    call.sentiment = body.sentiment
    call.disposition = body.disposition
    call.pipeline_completed = body.pipeline_completed
    call.transfer_count = body.transfer_count
    call.credits_used = body.credits_used
    call.duration_seconds = body.duration_seconds
    call.recording_key = body.recording_key
    call.cost_micros = body.cost_micros
    call.ended_at = now_utc()
    call.reserved_voice_seconds = 0
    call.reserved_tokens = 0
    call.reservation_expires_at = None
    call.details = {
        **call.details,
        "safetyEvents": list(body.safety_events),
        "tokensUsed": charged_tokens,
    }
    db.flush()
    db.add(
        AuditLog(
            workspace_id=workspace_id,
            actor="service:voice-worker",
            action="call.completed",
            resource_type="call",
            resource_id=str(call.id),
            details={
                "durationSeconds": body.duration_seconds,
                "sentiment": body.sentiment,
                "disposition": body.disposition,
                "pipelineCompleted": body.pipeline_completed,
                "transferCount": body.transfer_count,
                "safetyEvents": list(body.safety_events),
                "tokensUsed": charged_tokens,
            },
        )
    )
    prepare_post_call_jobs(db, call)
    db.flush()
    post_call = post_call_stats(db, call.id, workspace_id)
    db.commit()
    if post_call["pending"]:
        background_tasks.add_task(run_post_call_background, call.id, settings)
    return {
        "completed": True,
        "callId": call.id,
        "idempotent": False,
        "postCall": post_call,
    }
