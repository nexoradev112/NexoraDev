from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pydantic import Field as PydanticField
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..dependencies import (
    Principal,
    WorkspaceAccess,
    require_recent_auth,
    require_superadmin,
    require_workspace,
)
from ..models import AuditLog, StoredFile
from ..storage import (
    discard_saved_upload,
    enforce_workspace_storage_quota,
    resolve_storage_key,
    save_upload,
)

router = APIRouter(tags=["files"])


def _require_file_feature(access: WorkspaceAccess, category: str) -> None:
    assert access.license is not None
    feature = "recordings" if category == "recordings" else "agents"
    if feature not in access.license.features:
        raise HTTPException(status_code=403, detail=f"Feature '{feature}' is not included in this license")


def serialize_file(record: StoredFile) -> dict[str, object]:
    return {
        "id": record.id,
        "filename": record.filename,
        "category": record.category,
        "contentType": record.content_type,
        "size": record.size,
        "status": record.status,
        "locale": record.details.get("locale", "auto"),
        "durationMs": record.details.get("durationMs", 0),
        "transcript": record.details.get("transcript", ""),
        "safetyStatus": record.details.get("safetyStatus", "not_applicable"),
        "createdAt": record.created_at.isoformat(),
    }


def _assert_file_namespace(record: StoredFile) -> None:
    expected = f"{record.category}/w{record.workspace_id}/"
    if not record.storage_key.startswith(expected):
        raise HTTPException(status_code=404, detail="File not found")


@router.get("/api/files")
def list_files(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer"))],
    db: Annotated[Session, Depends(get_db)],
    category: Literal["knowledge", "recordings"] | None = None,
) -> dict[str, object]:
    if category:
        _require_file_feature(access, category)
    assert access.license is not None
    allowed_categories = [
        item
        for item, feature in (("knowledge", "agents"), ("recordings", "recordings"))
        if feature in access.license.features
    ]
    if not allowed_categories:
        return {"files": []}
    query = select(StoredFile).where(
        StoredFile.workspace_id == access.workspace.id,
        StoredFile.status == "ready",
        StoredFile.category.in_(allowed_categories),
    )
    if category:
        query = query.where(StoredFile.category == category)
    records = db.scalars(query.order_by(StoredFile.id.desc()).limit(200)).all()
    return {
        "files": [
            serialize_file(record)
            for record in records
            if record.storage_key.startswith(f"{record.category}/w{record.workspace_id}/")
        ]
    }


@router.post("/api/files", status_code=201)
async def upload_file(
    file: Annotated[UploadFile, File()],
    access: Annotated[WorkspaceAccess, Depends(require_workspace("member"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    category: Literal["knowledge", "recordings"] = "knowledge",
) -> dict[str, object]:
    feature = "recordings" if category == "recordings" else "agents"
    assert access.license is not None
    if feature not in access.license.features:
        raise HTTPException(status_code=403, detail=f"Feature '{feature}' is not included in this license")
    saved = await save_upload(file, category, access.workspace.id, settings)
    try:
        enforce_workspace_storage_quota(db, access.workspace.id, saved.size, settings)
        record = StoredFile(
            workspace_id=access.workspace.id,
            category=category,
            storage_key=saved.storage_key,
            filename=saved.filename,
            content_type=saved.content_type,
            size=saved.size,
            sha256=saved.sha256,
            details={"safetyStatus": "pending_review"} if category == "recordings" else {},
            created_by_user_id=access.user.id,
        )
        db.add(record)
        db.flush()
        db.add(
            AuditLog(
                workspace_id=access.workspace.id,
                actor=f"user:{access.user.id}",
                action="file.uploaded",
                resource_type="stored_file",
                resource_id=str(record.id),
                details={"category": category, "size": saved.size},
            )
        )
        db.commit()
    except BaseException:
        try:
            db.rollback()
        finally:
            discard_saved_upload(settings, saved)
        raise
    return {"file": serialize_file(record)}


@router.get("/api/files/{file_id}/content")
def download_file(
    file_id: int,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    record = db.scalar(
        select(StoredFile).where(
            StoredFile.id == file_id,
            StoredFile.workspace_id == access.workspace.id,
            StoredFile.status == "ready",
        )
    )
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    _require_file_feature(access, record.category)
    _assert_file_namespace(record)
    path = resolve_storage_key(settings, record.storage_key)
    return FileResponse(
        path,
        media_type=record.content_type,
        filename=record.filename,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/api/recordings")
def list_recordings(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer", "recordings"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    id: Annotated[int | None, Query(ge=1)] = None,
    content: bool = False,
    workspace: int | None = None,
) -> Any:
    if workspace is not None and workspace != access.workspace.id:
        raise HTTPException(status_code=404, detail="Recording not found")
    if id is not None and content:
        record = db.scalar(
            select(StoredFile).where(
                StoredFile.id == id,
                StoredFile.workspace_id == access.workspace.id,
                StoredFile.category == "recordings",
                StoredFile.status == "ready",
            )
        )
        if not record:
            raise HTTPException(status_code=404, detail="Recording not found")
        _assert_file_namespace(record)
        return FileResponse(
            resolve_storage_key(settings, record.storage_key),
            media_type=record.content_type,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
    rows = db.scalars(
        select(StoredFile)
        .where(
            StoredFile.workspace_id == access.workspace.id,
            StoredFile.category == "recordings",
            StoredFile.status == "ready",
        )
        .order_by(StoredFile.id.desc())
    ).all()
    return {
        "recordings": [
            serialize_file(row)
            for row in rows
            if row.storage_key.startswith(f"recordings/w{access.workspace.id}/")
        ]
    }


@router.post("/api/recordings", status_code=201)
async def upload_recording(
    file: Annotated[UploadFile, File()],
    access: Annotated[WorkspaceAccess, Depends(require_workspace("member", "recordings"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    locale: Annotated[str, Form()] = "auto",
    duration_ms: Annotated[int, Form(alias="durationMs", ge=0, le=86_400_000)] = 0,
    auto_transcribe: Annotated[bool, Form(alias="autoTranscribe")] = False,
) -> dict[str, object]:
    del auto_transcribe  # Transcription adapters are intentionally phase 2.
    if locale not in {"auto", "ar", "en-US", "en-GB", "hi-IN", "hi-en"}:
        raise HTTPException(status_code=422, detail="Recording locale is unsupported")
    saved = await save_upload(file, "recordings", access.workspace.id, settings)
    try:
        enforce_workspace_storage_quota(db, access.workspace.id, saved.size, settings)
        record = StoredFile(
            workspace_id=access.workspace.id,
            category="recordings",
            storage_key=saved.storage_key,
            filename=saved.filename,
            content_type=saved.content_type,
            size=saved.size,
            sha256=saved.sha256,
            details={
                "locale": locale,
                "durationMs": duration_ms,
                "transcript": "",
                "safetyStatus": "pending_review",
            },
            created_by_user_id=access.user.id,
        )
        db.add(record)
        db.commit()
    except BaseException:
        try:
            db.rollback()
        finally:
            discard_saved_upload(settings, saved)
        raise
    return {"recording": serialize_file(record)}


@router.delete("/api/recordings")
def delete_recording(
    id: Annotated[int, Query(ge=1)],
    access: Annotated[WorkspaceAccess, Depends(require_workspace("member", "recordings"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    record = db.scalar(
        select(StoredFile)
        .where(
            StoredFile.id == id,
            StoredFile.workspace_id == access.workspace.id,
            StoredFile.category == "recordings",
            StoredFile.status == "ready",
        )
        .with_for_update()
    )
    if not record:
        raise HTTPException(status_code=404, detail="Recording not found")
    _assert_file_namespace(record)
    path = resolve_storage_key(settings, record.storage_key)
    record.status = "deleted"
    db.add(
        AuditLog(
            workspace_id=access.workspace.id,
            actor=f"user:{access.user.id}",
            action="recording.deleted",
            resource_type="stored_file",
            resource_id=str(record.id),
        )
    )
    db.commit()
    try:
        path.unlink()
    except OSError:
        # A deleted row is never served again. A failed physical cleanup leaves
        # only an operator-recoverable orphan for the maintenance job/backup.
        pass
    return {"deleted": True, "id": record.id}


class AudioReviewBody(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str = PydanticField(default="", max_length=500)


@router.get("/api/superadmin/audio-review")
def list_audio_review(
    _principal: Annotated[Principal, Depends(require_superadmin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    rows = db.scalars(
        select(StoredFile)
        .where(StoredFile.category == "recordings", StoredFile.status == "ready")
        .order_by(StoredFile.id.desc())
        .limit(200)
    ).all()
    return {"recordings": [serialize_file(row) | {"workspaceId": row.workspace_id} for row in rows]}


@router.get("/api/superadmin/audio-review/{file_id}/content")
def audio_review_content(
    file_id: int,
    _principal: Annotated[Principal, Depends(require_superadmin)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    row = db.scalar(
        select(StoredFile).where(
            StoredFile.id == file_id,
            StoredFile.category == "recordings",
            StoredFile.status == "ready",
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    _assert_file_namespace(row)
    return FileResponse(
        resolve_storage_key(settings, row.storage_key),
        media_type=row.content_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/api/superadmin/audio-review/{file_id}")
def review_audio(
    file_id: int,
    body: AudioReviewBody,
    principal: Annotated[Principal, Depends(require_superadmin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    require_recent_auth(principal)
    row = db.scalar(
        select(StoredFile)
        .where(
            StoredFile.id == file_id,
            StoredFile.category == "recordings",
            StoredFile.status == "ready",
        )
        .with_for_update()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Recording not found")
    if body.decision == "approved" and row.content_type not in {"audio/wav", "audio/x-wav"}:
        raise HTTPException(
            status_code=422,
            detail="Only reviewed WAV files can be approved for runtime playback",
        )
    row.details = {
        **row.details,
        "safetyStatus": body.decision,
        "reviewNote": body.note.strip(),
        "reviewedByUserId": principal.user.id,
    }
    db.add(
        AuditLog(
            workspace_id=row.workspace_id,
            actor=f"superadmin:{principal.user.id}",
            action=f"audio.{body.decision}",
            resource_type="stored_file",
            resource_id=str(row.id),
            details={"contentType": row.content_type},
        )
    )
    db.commit()
    return {"recording": serialize_file(row)}
