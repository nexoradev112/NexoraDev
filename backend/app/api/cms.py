from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..dependencies import Principal, require_recent_auth, require_superadmin
from ..models import AuditLog, CmsMedia, CmsSetting
from ..storage import discard_saved_upload, resolve_storage_key, save_upload

router = APIRouter(tags=["cms"])

DEFAULT_SETTINGS: dict[str, Any] = {
    "brand.name": "Nexora",
    "brand.tagline": "Build, test and operate AI agents",
    "brand.primaryColor": "#7357ff",
    "homepage.heroTitle": "Production-ready voice and chat agents",
    "homepage.heroBody": "Design multilingual agent workflows with provider choice and built-in safety.",
}
ALLOWED_KEYS = set(DEFAULT_SETTINGS) | {
    "brand.logoMediaId",
    "homepage.ctaLabel",
    "homepage.ctaHref",
    "seo.title",
    "seo.description",
}


@router.get("/api/cms/settings")
def get_cms_settings(db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    rows = db.scalars(select(CmsSetting)).all()
    values = dict(DEFAULT_SETTINGS)
    values.update({row.key: row.value for row in rows})
    return {"settings": values}


class CmsSettingsBody(BaseModel):
    settings: dict[str, Any]

    @field_validator("settings")
    @classmethod
    def validate_settings(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value or set(value) - ALLOWED_KEYS:
            raise ValueError("CMS settings contain an unsupported key")
        if len(json.dumps(value, ensure_ascii=False)) > 32_000:
            raise ValueError("CMS settings are too large")
        for key, item in value.items():
            if key == "brand.logoMediaId":
                if item is not None and (not isinstance(item, int) or item < 1):
                    raise ValueError("Logo media id is invalid")
            elif not isinstance(item, str) or len(item) > 4_000:
                raise ValueError("CMS setting value is invalid")
        return value


@router.post("/api/cms/settings")
def update_cms_settings(
    body: CmsSettingsBody,
    principal: Annotated[Principal, Depends(require_superadmin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    require_recent_auth(principal)
    for key, value in body.settings.items():
        row = db.get(CmsSetting, key)
        if row:
            row.value = value
        else:
            db.add(CmsSetting(key=key, value=value, group_name=key.split(".", 1)[0]))
    db.add(
        AuditLog(
            workspace_id=None,
            actor=f"superadmin:{principal.user.id}",
            action="cms.settings_updated",
            resource_type="cms_settings",
            details={"keys": sorted(body.settings)},
        )
    )
    db.commit()
    return get_cms_settings(db)


def serialize_media(record: CmsMedia) -> dict[str, object]:
    return {
        "id": record.id,
        "filename": record.filename,
        "contentType": record.content_type,
        "size": record.size,
        "alt": record.alt,
        "url": f"/api/cms/media/{record.id}/content",
        "createdAt": record.created_at.isoformat(),
    }


@router.get("/api/cms/media")
def list_cms_media(db: Annotated[Session, Depends(get_db)]) -> dict[str, object]:
    rows = db.scalars(select(CmsMedia).order_by(CmsMedia.id.desc()).limit(200)).all()
    return {"media": [serialize_media(row) for row in rows]}


@router.post("/api/cms/media", status_code=201)
async def upload_cms_media(
    file: Annotated[UploadFile, File()],
    principal: Annotated[Principal, Depends(require_superadmin)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    alt: str = "",
) -> dict[str, object]:
    require_recent_auth(principal)
    if len(alt) > 300:
        raise HTTPException(status_code=422, detail="Alt text is too long")
    saved = await save_upload(file, "cms", 0, settings)
    try:
        record = CmsMedia(
            storage_key=saved.storage_key,
            filename=saved.filename,
            content_type=saved.content_type,
            size=saved.size,
            sha256=saved.sha256,
            alt=alt.strip(),
            created_by_user_id=principal.user.id,
        )
        db.add(record)
        db.flush()
        db.add(
            AuditLog(
                workspace_id=None,
                actor=f"superadmin:{principal.user.id}",
                action="cms.media_uploaded",
                resource_type="cms_media",
                resource_id=str(record.id),
                details={"size": record.size, "contentType": record.content_type},
            )
        )
        db.commit()
    except BaseException:
        try:
            db.rollback()
        finally:
            discard_saved_upload(settings, saved)
        raise
    return {"media": serialize_media(record)}


@router.get("/api/cms/media/{media_id}/content")
def cms_media_content(
    media_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    record = db.get(CmsMedia, media_id)
    if not record or not record.storage_key.startswith("cms/w0/"):
        raise HTTPException(status_code=404, detail="Media not found")
    path = resolve_storage_key(settings, record.storage_key)
    return FileResponse(
        path,
        media_type=record.content_type,
        headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )
