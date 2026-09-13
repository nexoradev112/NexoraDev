from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Workspace(Base, TimestampMixin):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending_license", nullable=False)
    region: Mapped[str] = mapped_column(String(32), default="local", nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_membership_workspace_user"),
        Index("ix_memberships_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(24), default="member", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    user_agent_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class License(Base, TimestampMixin):
    __tablename__ = "licenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    plan: Mapped[str] = mapped_column(String(64), nullable=False)
    seats: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="unused", nullable=False, index=True)
    provider_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    hybrid_policy: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    quotas: Mapped[dict[str, int | None]] = mapped_column(JSON, default=dict, nullable=False)
    features: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    signed_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    state_signature: Mapped[str] = mapped_column(Text, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class WorkspaceInvite(Base):
    __tablename__ = "workspace_invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(24), default="member", nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invited_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProviderConnection(Base, TimestampMixin):
    __tablename__ = "provider_connections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "kind", "provider", name="uq_provider_workspace_kind_provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    secret_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    config: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)


class Integration(Base, TimestampMixin):
    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_integration_workspace_name"),
        Index("ix_integrations_workspace_updated", "workspace_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(24), default="none", nullable=False)
    encrypted_secret: Mapped[str | None] = mapped_column(Text)
    secret_nonce: Mapped[str | None] = mapped_column(String(64))
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    config: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)


class Agent(Base, TimestampMixin):
    __tablename__ = "agents"
    __table_args__ = (Index("ix_agents_workspace_updated", "workspace_id", "updated_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    objective: Mapped[str] = mapped_column(Text, default="", nullable=False)
    global_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    greeting: Mapped[str] = mapped_column(Text, default="Hello, how can I help today?", nullable=False)
    channel: Mapped[str] = mapped_column(String(24), default="voice", nullable=False)
    model: Mapped[str] = mapped_column(String(100), default="gpt-4.1-mini", nullable=False)
    voice: Mapped[str] = mapped_column(String(100), default="elevenlabs-rachel", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    recording_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_call_seconds: Mapped[int] = mapped_column(Integer, default=1800, nullable=False)
    human_handoff_number: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    locale: Mapped[str] = mapped_column(String(16), default="en-US", nullable=False)
    provider_policy: Mapped[dict[str, list[str]]] = mapped_column(
        JSON,
        default=lambda: {
            "llm": ["openai"],
            "stt": ["deepgram"],
            "tts": ["elevenlabs"],
            "realtime": ["livekit"],
        },
        nullable=False,
    )
    workflow: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=lambda: {"nodes": [], "edges": []}, nullable=False
    )


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("workspace_id", "agent_id", "version", name="uq_agent_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StoredFile(Base):
    __tablename__ = "stored_files"
    __table_args__ = (Index("ix_stored_files_workspace_category", "workspace_id", "category"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="ready", nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CmsSetting(Base):
    __tablename__ = "cms_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    group_name: Mapped[str] = mapped_column(String(64), default="general", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class CmsMedia(Base):
    __tablename__ = "cms_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    storage_key: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    alt: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CallSession(Base, TimestampMixin):
    __tablename__ = "call_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    license_id: Mapped[int] = mapped_column(ForeignKey("licenses.id", ondelete="RESTRICT"), nullable=False)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False)
    room_name: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    direction: Mapped[str] = mapped_column(String(16), default="test", nullable=False)
    channel: Mapped[str] = mapped_column(String(16), default="voice", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    to_number: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    from_number: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    provider: Mapped[str] = mapped_column(String(64), default="livekit", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_voice_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reservation_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    transcript: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sentiment: Mapped[str | None] = mapped_column(String(20))
    disposition: Mapped[str | None] = mapped_column(String(32))
    pipeline_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    transfer_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    credits_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_micros: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recording_key: Mapped[str | None] = mapped_column(String(500))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class PostCallResult(Base, TimestampMixin):
    __tablename__ = "post_call_results"
    __table_args__ = (
        UniqueConstraint("workspace_id", "call_id", "node_id", name="uq_post_call_result_node"),
        Index("ix_post_call_results_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    call_id: Mapped[int] = mapped_column(
        ForeignKey("call_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    integration_id: Mapped[int | None] = mapped_column(
        ForeignKey("integrations.id", ondelete="SET NULL"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), default="", nullable=False)


class LiveKitRoomTerminationJob(Base, TimestampMixin):
    __tablename__ = "livekit_room_termination_jobs"
    __table_args__ = (
        UniqueConstraint("room_name", "reason_code", name="uq_room_termination_reason"),
        Index("ix_room_termination_due", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    call_id: Mapped[int] = mapped_column(
        ForeignKey("call_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    room_name: Mapped[str] = mapped_column(String(120), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_error_code: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PhoneNumber(Base):
    __tablename__ = "phone_numbers"
    __table_args__ = (UniqueConstraint("workspace_id", "e164", name="uq_phone_workspace_e164"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_ref: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    e164: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(100), default="Main line", nullable=False)
    direction: Mapped[str] = mapped_column(String(16), default="both", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    license_id: Mapped[int | None] = mapped_column(ForeignKey("licenses.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UsageCounter(Base):
    __tablename__ = "usage_counters"
    __table_args__ = (UniqueConstraint("license_id", "unit", name="uq_usage_counter_license_unit"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    license_id: Mapped[int] = mapped_column(ForeignKey("licenses.id", ondelete="CASCADE"), index=True)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ConsentRecord(Base):
    __tablename__ = "consent_records"
    __table_args__ = (UniqueConstraint("workspace_id", "phone_hash", "channel", name="uq_consent_lookup"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), default="voice", nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    legal_basis: Mapped[str] = mapped_column(String(32), default="consent", nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class SuppressionEntry(Base):
    __tablename__ = "suppression_entries"
    __table_args__ = (UniqueConstraint("workspace_id", "phone_hash", name="uq_suppression_lookup"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"), index=True
    )
    actor: Mapped[str] = mapped_column(String(160), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class WorkerRequestNonce(Base):
    __tablename__ = "worker_request_nonces"

    nonce: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_path: Mapped[str] = mapped_column(String(300), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
