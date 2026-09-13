from __future__ import annotations

import re
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..dependencies import Principal, current_principal
from ..licensing import active_license_for_workspace, assert_seat_available, require_feature, token_hash
from ..models import AuditLog, Membership, User, UserSession, Workspace, WorkspaceInvite
from ..security import (
    DUMMY_PASSWORD_HASH,
    assert_auth_rate_limit,
    authenticate_session,
    clear_session_cookie,
    create_session,
    hash_password,
    normalize_email,
    now_utc,
    release_auth_attempt,
    set_session_cookie,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterBody(BaseModel):
    email: str
    password: str
    name: str = Field(min_length=1, max_length=120)
    workspace_name: str | None = Field(default=None, min_length=2, max_length=160, alias="workspaceName")
    invite_token: str | None = Field(default=None, max_length=512, alias="inviteToken")


class LoginBody(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=256)


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:70]
    return result or "workspace"


def _workspace_summary(db: Session, user: User) -> list[dict[str, object]]:
    rows = db.execute(
        select(Workspace, Membership)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(Membership.user_id == user.id)
        .order_by(Workspace.id)
    ).all()
    return [
        {
            "id": workspace.id,
            "name": workspace.name,
            "slug": workspace.slug,
            "status": workspace.status,
            "role": membership.role,
        }
        for workspace, membership in rows
    ]


@router.post("/register", status_code=201)
def register(
    body: RegisterBody,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    email = normalize_email(body.email)
    ip_attempt = assert_auth_rate_limit(request, "register-ip", "*", limit=20, window_seconds=3600)
    email_attempt = assert_auth_rate_limit(request, "register", email, limit=5, window_seconds=3600)
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    invite: WorkspaceInvite | None = None
    if body.invite_token:
        invite = db.scalar(
            select(WorkspaceInvite)
            .where(WorkspaceInvite.token_hash == token_hash(body.invite_token.strip()))
            .with_for_update()
        )
        if (
            not invite
            or invite.accepted_at
            or invite.revoked_at
            or invite.expires_at.replace(tzinfo=invite.expires_at.tzinfo or now_utc().tzinfo) <= now_utc()
            or invite.email != email
        ):
            raise HTTPException(status_code=400, detail="Invite is invalid or expired")
        # Lock the tenant row so two invite acceptances cannot consume the final seat.
        db.scalar(select(Workspace).where(Workspace.id == invite.workspace_id).with_for_update())
        claims = active_license_for_workspace(db, invite.workspace_id, settings, lock=True)
        require_feature(claims, "members")
        assert_seat_available(db, claims, include_pending_invite=False)
    elif not settings.ALLOW_PUBLIC_TENANT_REGISTRATION:
        raise HTTPException(status_code=403, detail="Tenant registration is disabled")
    elif not body.workspace_name:
        raise HTTPException(status_code=422, detail="workspaceName is required")

    user = User(email=email, name=body.name.strip(), password_hash=hash_password(body.password))
    db.add(user)
    db.flush()
    if invite:
        workspace = db.get(Workspace, invite.workspace_id)
        if not workspace:
            raise HTTPException(status_code=400, detail="Invite workspace is unavailable")
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role=invite.role))
        invite.accepted_at = now_utc()
    else:
        assert body.workspace_name is not None
        base = _slug(body.workspace_name)
        slug = base
        while db.scalar(select(Workspace.id).where(Workspace.slug == slug)):
            slug = f"{base[:60]}-{secrets.token_hex(3)}"
        workspace = Workspace(name=body.workspace_name.strip(), slug=slug)
        db.add(workspace)
        db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
    db.add(
        AuditLog(
            workspace_id=workspace.id,
            actor=f"user:{user.id}",
            action="account.registered",
            resource_type="user",
            resource_id=str(user.id),
        )
    )
    raw_session = create_session(db, user, request, settings)
    db.commit()
    release_auth_attempt(ip_attempt)
    release_auth_attempt(email_attempt)
    set_session_cookie(response, raw_session, settings)
    return {"user": {"id": user.id, "email": user.email, "name": user.name}, "workspaceId": workspace.id}


@router.post("/login")
def login(
    body: LoginBody,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    email = normalize_email(body.email)
    ip_attempt = assert_auth_rate_limit(request, "login-ip", "*", limit=60, window_seconds=900)
    email_attempt = assert_auth_rate_limit(request, "login", email, limit=10, window_seconds=900)
    user = db.scalar(select(User).where(User.email == email))
    password_valid = verify_password(user.password_hash if user else DUMMY_PASSWORD_HASH, body.password)
    if not user or user.status != "active" or not password_valid:
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
    raw_session = create_session(db, user, request, settings)
    db.add(
        AuditLog(
            workspace_id=None,
            actor=f"user:{user.id}",
            action="auth.login",
            resource_type="session",
        )
    )
    db.commit()
    release_auth_attempt(ip_attempt)
    release_auth_attempt(email_attempt)
    set_session_cookie(response, raw_session, settings)
    return {
        "user": {"id": user.id, "email": user.email, "name": user.name, "isSuperadmin": user.is_superadmin}
    }


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, bool]:
    try:
        _user, session = authenticate_session(db, request, settings)
        session.revoked_at = now_utc()
        db.commit()
    except HTTPException:
        db.rollback()
    clear_session_cookie(response, settings)
    return {"ok": True}


class PasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256, alias="currentPassword")
    new_password: str = Field(min_length=12, max_length=256, alias="newPassword")


@router.post("/password")
def change_password(
    body: PasswordBody,
    response: Response,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, bool]:
    if not verify_password(principal.user.password_hash, body.current_password):
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    changed_at = now_utc()
    principal.user.password_hash = hash_password(body.new_password)
    principal.user.password_changed_at = changed_at
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == principal.user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=changed_at)
    )
    db.add(
        AuditLog(
            workspace_id=None,
            actor=f"user:{principal.user.id}",
            action="auth.password_changed",
            resource_type="user",
            resource_id=str(principal.user.id),
        )
    )
    db.commit()
    clear_session_cookie(response, settings)
    return {"changed": True}


@router.get("/me")
def me(
    principal: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    user = principal.user
    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "isSuperadmin": user.is_superadmin,
        },
        "workspaces": _workspace_summary(db, user),
    }
