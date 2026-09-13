from __future__ import annotations

import ipaddress
import re
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..dependencies import WorkspaceAccess, require_recent_auth, require_workspace
from ..integration_vault import encrypt_integration_secret
from ..models import AuditLog, Integration, PostCallResult

router = APIRouter(tags=["integrations"])

INTEGRATION_KINDS = {"webhook", "crm", "calendar", "database", "custom-api"}
AUTH_TYPES = {"bearer", "api-key", "basic"}
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,80}$")
FORBIDDEN_AUTH_HEADERS = {
    "accept",
    "authorization",
    "connection",
    "content-length",
    "content-type",
    "host",
    "idempotency-key",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "user-agent",
    "x-nexora-signature",
    "x-nexora-timestamp",
}


def canonical_https_origin(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Integration base URL is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=422, detail="Integration base URL must be an exact HTTPS origin")
    try:
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise HTTPException(status_code=422, detail="Integration hostname is invalid") from exc
    if (
        not host
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
        or any(not label or len(label) > 63 for label in host.split("."))
    ):
        raise HTTPException(status_code=422, detail="Integration hostname is not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # DNS is checked again immediately before every delivery. Requiring a
        # hostname here also prevents a tenant from saving an IP-literal bypass.
        raise HTTPException(status_code=422, detail="Integration hostname must not be an IP address")
    return f"https://{host}"


def public_integration(row: Integration) -> dict[str, object]:
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "baseUrl": row.base_url,
        "authType": row.auth_type,
        "config": row.config,
        "status": row.status,
        "hasSecret": bool(row.encrypted_secret and row.secret_nonce),
        "createdAt": row.created_at.isoformat(),
        "updatedAt": row.updated_at.isoformat(),
    }


class IntegrationBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=2, max_length=80)
    kind: str = Field(default="webhook", min_length=2, max_length=32)
    base_url: str = Field(min_length=10, max_length=500, alias="baseUrl")
    auth_type: str = Field(default="bearer", min_length=2, max_length=24, alias="authType")
    secret: str | None = Field(default=None, max_length=4_096)
    config: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("Integration name is too short")
        return value

    @field_validator("kind")
    @classmethod
    def supported_kind(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in INTEGRATION_KINDS:
            raise ValueError("Integration kind is unsupported")
        return value

    @field_validator("auth_type")
    @classmethod
    def supported_auth(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in AUTH_TYPES:
            raise ValueError("Integration authentication type is unsupported")
        return value

    @field_validator("config")
    @classmethod
    def safe_config(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) - {"headerName", "timeoutMs"}:
            raise ValueError("Integration config contains an unsupported field")
        if any(not isinstance(item, str) or len(item) > 120 for item in value.values()):
            raise ValueError("Integration config contains an invalid value")
        if "headerName" in value and (
            not HEADER_NAME_RE.fullmatch(value["headerName"])
            or value["headerName"].lower() in FORBIDDEN_AUTH_HEADERS
        ):
            raise ValueError("Integration API-key header name is invalid or reserved")
        if "timeoutMs" in value:
            try:
                timeout = int(value["timeoutMs"])
            except ValueError as exc:
                raise ValueError("Integration timeout must be an integer") from exc
            if not 1_000 <= timeout <= 5_000:
                raise ValueError("Integration timeout must be between 1000 and 5000ms")
        return dict(value)


@router.get("/api/integrations")
def list_integrations(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("admin", "post_call"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    rows = db.scalars(
        select(Integration)
        .where(Integration.workspace_id == access.workspace.id)
        .order_by(Integration.updated_at.desc())
        .limit(100)
    ).all()
    return {"integrations": [public_integration(row) for row in rows]}


@router.post("/api/integrations", status_code=201)
def save_integration(
    body: IntegrationBody,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("admin", "post_call"))],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    require_recent_auth(access)
    base_url = canonical_https_origin(body.base_url)
    secret = body.secret or ""
    if any(ord(character) < 32 or ord(character) == 127 for character in secret):
        raise HTTPException(status_code=422, detail="Integration credential contains invalid characters")
    row = db.scalar(
        select(Integration)
        .where(Integration.workspace_id == access.workspace.id, Integration.name == body.name)
        .with_for_update()
    )
    if not row:
        if len(secret) < 16:
            raise HTTPException(
                status_code=422, detail="Integration credential must contain at least 16 characters"
            )
        row = Integration(
            workspace_id=access.workspace.id,
            name=body.name,
            kind=body.kind,
            base_url=base_url,
            auth_type=body.auth_type,
            key_version=settings.CREDENTIAL_KEY_VERSION,
            config=dict(body.config),
            status="active",
        )
        db.add(row)
        db.flush()
    else:
        row.kind = body.kind
        row.base_url = base_url
        row.auth_type = body.auth_type
        row.config = dict(body.config)
        row.status = "active"
        if not secret and not row.encrypted_secret:
            raise HTTPException(status_code=422, detail="Integration credential is required")

    if secret:
        if len(secret) < 16:
            raise HTTPException(
                status_code=422, detail="Integration credential must contain at least 16 characters"
            )
        row.key_version = settings.CREDENTIAL_KEY_VERSION
        encrypt_integration_secret(row, secret, settings)
    db.add(
        AuditLog(
            workspace_id=access.workspace.id,
            actor=f"user:{access.user.id}",
            action="integration.saved",
            resource_type="integration",
            resource_id=str(row.id),
            details={"kind": row.kind, "host": urlsplit(row.base_url).hostname},
        )
    )
    db.commit()
    return {"integration": public_integration(row)}


@router.delete("/api/integrations/{integration_id}")
def disable_integration(
    integration_id: int,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("admin", "post_call"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, bool]:
    require_recent_auth(access)
    row = db.scalar(
        select(Integration)
        .where(
            Integration.id == integration_id,
            Integration.workspace_id == access.workspace.id,
        )
        .with_for_update()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Integration not found")
    row.status = "disabled"
    row.encrypted_secret = None
    row.secret_nonce = None
    db.add(
        AuditLog(
            workspace_id=access.workspace.id,
            actor=f"user:{access.user.id}",
            action="integration.disabled",
            resource_type="integration",
            resource_id=str(row.id),
            details={"kind": row.kind},
        )
    )
    db.commit()
    return {"ok": True}


@router.get("/api/evaluations")
def list_evaluations(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer", "analytics"))],
    db: Annotated[Session, Depends(get_db)],
    call_id: Annotated[int | None, Query(alias="callId", ge=1)] = None,
) -> dict[str, object]:
    query = select(PostCallResult).where(
        PostCallResult.workspace_id == access.workspace.id,
        PostCallResult.kind == "qa",
    )
    if call_id is not None:
        query = query.where(PostCallResult.call_id == call_id)
    rows = db.scalars(query.order_by(PostCallResult.created_at.desc()).limit(200)).all()
    return {
        "evaluations": [
            {
                "id": row.id,
                "callId": row.call_id,
                "agentId": row.agent_id,
                "nodeId": row.node_id,
                "status": row.status,
                "provider": row.provider,
                "model": row.model,
                "result": row.result,
                "errorCode": row.error_code,
                "createdAt": row.created_at.isoformat(),
                "updatedAt": row.updated_at.isoformat(),
            }
            for row in rows
        ]
    }
