from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .db import SessionLocal
from .livekit_admin import terminate_livekit_room
from .models import CallSession, LiveKitRoomTerminationJob
from .security import now_utc


def enqueue_room_termination(db: Session, call: CallSession, reason_code: str) -> None:
    """Persist teardown in the same transaction that cancels the call."""

    existing = db.scalar(
        select(LiveKitRoomTerminationJob.id).where(
            LiveKitRoomTerminationJob.room_name == call.room_name,
            LiveKitRoomTerminationJob.reason_code == reason_code,
        )
    )
    if existing is not None:
        return
    db.add(
        LiveKitRoomTerminationJob(
            workspace_id=call.workspace_id,
            call_id=call.id,
            room_name=call.room_name,
            reason_code=reason_code[:64],
            status="pending",
            next_attempt_at=now_utc(),
        )
    )


def run_pending_room_terminations(settings: Settings, limit: int = 20) -> int:
    """Retry room deletion indefinitely with bounded exponential backoff."""

    processed = 0
    with SessionLocal() as db:
        rows = db.scalars(
            select(LiveKitRoomTerminationJob)
            .where(
                LiveKitRoomTerminationJob.status == "pending",
                LiveKitRoomTerminationJob.next_attempt_at <= now_utc(),
            )
            .order_by(LiveKitRoomTerminationJob.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            row.status = "running"
            row.attempts += 1
            db.commit()
            try:
                asyncio.run(terminate_livekit_room(row.room_name, settings))
            except Exception:  # noqa: BLE001
                # Never persist raw SDK/provider errors: they may contain URLs
                # or credentials. An operator sees only the stable error code.
                row.status = "pending"
                row.last_error_code = "livekit_room_delete_failed"
                delay = min(3_600, 2 ** min(row.attempts, 11))
                row.next_attempt_at = now_utc() + timedelta(seconds=delay)
                if row.attempts >= 8 and row.alerted_at is None:
                    row.alerted_at = now_utc()
            else:
                row.status = "succeeded"
                row.last_error_code = ""
                row.completed_at = now_utc()
            db.commit()
            processed += 1
    return processed


def serialize_termination_job(job: LiveKitRoomTerminationJob) -> dict[str, object]:
    return {
        "id": job.id,
        "workspaceId": job.workspace_id,
        "callId": job.call_id,
        "roomName": job.room_name,
        "reason": job.reason_code,
        "status": job.status,
        "attempts": job.attempts,
        "lastErrorCode": job.last_error_code or None,
        "alertedAt": job.alerted_at.isoformat() if job.alerted_at else None,
        "nextAttemptAt": job.next_attempt_at.isoformat(),
    }
