from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..dependencies import (
    Principal,
    WorkspaceAccess,
    current_principal,
    require_recent_auth,
    require_workspace,
)
from ..licensing import active_license_for_workspace, assert_seat_available, require_feature, token_hash
from ..models import AuditLog, Membership, User, Workspace, WorkspaceInvite
from ..security import normalize_email, now_utc

router = APIRouter(tags=["workspaces"])


def _accesses(db: Session, user_id: int) -> list[tuple[Workspace, Membership]]:
    return list(
        db.execute(
            select(Workspace, Membership)
            .join(Membership, Membership.workspace_id == Workspace.id)
            .where(Membership.user_id == user_id)
            .order_by(Workspace.name)
        ).all()
    )


@router.get("/api/workspaces")
def list_workspaces(
    principal: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    rows = _accesses(db, principal.user.id)
    return {
        "workspaces": [
            {
                "id": workspace.id,
                "name": workspace.name,
                "slug": workspace.slug,
                "status": workspace.status,
                "role": membership.role,
            }
            for workspace, membership in rows
        ],
        "currentWorkspaceId": rows[0][0].id if len(rows) == 1 else None,
    }


@router.get("/api/members")
def list_members(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("viewer", "members"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    rows = db.execute(
        select(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.workspace_id == access.workspace.id)
        .order_by(User.email)
    ).all()
    return {
        "members": [
            {
                "id": user.id,
                "email": user.email,
                "name": user.name,
                "role": membership.role,
                "status": user.status,
                "createdAt": membership.created_at.isoformat(),
            }
            for user, membership in rows
        ]
    }


class InviteBody(BaseModel):
    email: str
    role: str = Field(default="member", pattern="^(admin|operator|member|viewer)$")
    expires_hours: int = Field(default=72, ge=1, le=24 * 14, alias="expiresHours")


@router.get("/api/invites")
@router.get("/api/workspaces/invites")
def list_invites(
    access: Annotated[WorkspaceAccess, Depends(require_workspace("admin", "members"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    invites = db.scalars(
        select(WorkspaceInvite)
        .where(WorkspaceInvite.workspace_id == access.workspace.id)
        .order_by(WorkspaceInvite.id.desc())
    ).all()
    return {
        "invites": [
            {
                "id": invite.id,
                "email": invite.email,
                "role": invite.role,
                "expiresAt": invite.expires_at.isoformat(),
                "acceptedAt": invite.accepted_at.isoformat() if invite.accepted_at else None,
                "revokedAt": invite.revoked_at.isoformat() if invite.revoked_at else None,
            }
            for invite in invites
        ]
    }


@router.post("/api/invites", status_code=201)
@router.post("/api/workspaces/invites", status_code=201)
def create_invite(
    body: InviteBody,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("admin", "members"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    require_recent_auth(access)
    if body.role == "admin" and access.membership.role != "owner":
        raise HTTPException(status_code=403, detail="Only an owner can invite administrators")
    email = normalize_email(body.email)
    # Serialise seat allocation by tenant. PostgreSQL row locks make the
    # membership+pending invite count atomic.
    db.scalar(select(Workspace).where(Workspace.id == access.workspace.id).with_for_update())
    assert access.license is not None
    assert_seat_available(db, access.license)
    existing_member = db.scalar(
        select(Membership.id)
        .join(User, User.id == Membership.user_id)
        .where(Membership.workspace_id == access.workspace.id, User.email == email)
    )
    if existing_member:
        raise HTTPException(status_code=409, detail="User is already a workspace member")
    existing_invite = db.scalar(
        select(WorkspaceInvite.id).where(
            WorkspaceInvite.workspace_id == access.workspace.id,
            WorkspaceInvite.email == email,
            WorkspaceInvite.accepted_at.is_(None),
            WorkspaceInvite.revoked_at.is_(None),
            WorkspaceInvite.expires_at > now_utc(),
        )
    )
    if existing_invite:
        raise HTTPException(status_code=409, detail="An active invite already exists for this email")
    raw = "inv_" + secrets.token_urlsafe(32)
    invite = WorkspaceInvite(
        workspace_id=access.workspace.id,
        email=email,
        role=body.role,
        token_hash=token_hash(raw),
        expires_at=now_utc() + timedelta(hours=body.expires_hours),
        invited_by_user_id=access.user.id,
    )
    db.add(invite)
    db.add(
        AuditLog(
            workspace_id=access.workspace.id,
            actor=f"user:{access.user.id}",
            action="member.invited",
            resource_type="invite",
            details={"email": email, "role": body.role},
        )
    )
    db.commit()
    # The invite bearer token is returned only on creation; lists never expose it.
    return {
        "invite": {
            "id": invite.id,
            "email": invite.email,
            "role": invite.role,
            "expiresAt": invite.expires_at.isoformat(),
        },
        "inviteToken": raw,
    }


class AcceptInviteBody(BaseModel):
    token: str = Field(min_length=20, max_length=512)


@router.post("/api/invites/accept")
def accept_invite(
    body: AcceptInviteBody,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    invite = db.scalar(
        select(WorkspaceInvite)
        .where(WorkspaceInvite.token_hash == token_hash(body.token.strip()))
        .with_for_update()
    )
    if (
        not invite
        or invite.email != principal.user.email
        or invite.accepted_at
        or invite.revoked_at
        or invite.expires_at.replace(tzinfo=invite.expires_at.tzinfo or now_utc().tzinfo) <= now_utc()
    ):
        raise HTTPException(status_code=400, detail="Invite is invalid or expired")
    workspace = db.scalar(select(Workspace).where(Workspace.id == invite.workspace_id).with_for_update())
    if not workspace:
        raise HTTPException(status_code=400, detail="Invite workspace is unavailable")
    claims = active_license_for_workspace(db, workspace.id, settings)
    require_feature(claims, "members")
    if db.scalar(
        select(Membership.id).where(
            Membership.workspace_id == workspace.id,
            Membership.user_id == principal.user.id,
        )
    ):
        invite.accepted_at = now_utc()
        db.commit()
        return {"workspaceId": workspace.id, "accepted": True, "alreadyMember": True}
    assert_seat_available(db, claims, include_pending_invite=False)
    db.add(Membership(workspace_id=workspace.id, user_id=principal.user.id, role=invite.role))
    invite.accepted_at = now_utc()
    db.add(
        AuditLog(
            workspace_id=workspace.id,
            actor=f"user:{principal.user.id}",
            action="member.joined",
            resource_type="membership",
            details={"role": invite.role},
        )
    )
    db.commit()
    return {"workspaceId": workspace.id, "accepted": True}


class MemberRoleBody(BaseModel):
    role: str = Field(pattern="^(admin|operator|member|viewer)$")


def _manageable_membership(db: Session, access: WorkspaceAccess, user_id: int) -> Membership:
    membership = db.scalar(
        select(Membership)
        .where(
            Membership.workspace_id == access.workspace.id,
            Membership.user_id == user_id,
        )
        .with_for_update()
    )
    if not membership:
        raise HTTPException(status_code=404, detail="Workspace member not found")
    if membership.user_id == access.user.id:
        raise HTTPException(status_code=403, detail="Use another owner or administrator for your own access")
    if membership.role == "owner":
        raise HTTPException(status_code=403, detail="Workspace owner access cannot be changed here")
    if access.membership.role != "owner" and membership.role == "admin":
        raise HTTPException(status_code=403, detail="Only an owner can manage administrators")
    return membership


@router.patch("/api/members/{user_id}")
def update_member_role(
    user_id: int,
    body: MemberRoleBody,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("admin", "members"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    require_recent_auth(access)
    if body.role == "admin" and access.membership.role != "owner":
        raise HTTPException(status_code=403, detail="Only an owner can promote administrators")
    membership = _manageable_membership(db, access, user_id)
    membership.role = body.role
    db.add(
        AuditLog(
            workspace_id=access.workspace.id,
            actor=f"user:{access.user.id}",
            action="member.role_changed",
            resource_type="membership",
            resource_id=str(membership.id),
            details={"userId": user_id, "role": body.role},
        )
    )
    db.commit()
    return {"updated": True, "userId": user_id, "role": body.role}


@router.delete("/api/members/{user_id}")
def remove_member(
    user_id: int,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("admin", "members"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    require_recent_auth(access)
    membership = _manageable_membership(db, access, user_id)
    membership_id = membership.id
    db.delete(membership)
    db.add(
        AuditLog(
            workspace_id=access.workspace.id,
            actor=f"user:{access.user.id}",
            action="member.removed",
            resource_type="membership",
            resource_id=str(membership_id),
            details={"userId": user_id},
        )
    )
    db.commit()
    return {"removed": True, "userId": user_id}


@router.delete("/api/invites/{invite_id}")
def revoke_invite(
    invite_id: int,
    access: Annotated[WorkspaceAccess, Depends(require_workspace("admin", "members"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    require_recent_auth(access)
    invite = db.scalar(
        select(WorkspaceInvite)
        .where(
            WorkspaceInvite.id == invite_id,
            WorkspaceInvite.workspace_id == access.workspace.id,
        )
        .with_for_update()
    )
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")
    if invite.accepted_at:
        raise HTTPException(status_code=409, detail="Accepted invites cannot be revoked")
    if not invite.revoked_at:
        invite.revoked_at = now_utc()
        db.add(
            AuditLog(
                workspace_id=access.workspace.id,
                actor=f"user:{access.user.id}",
                action="invite.revoked",
                resource_type="invite",
                resource_id=str(invite.id),
                details={"email": invite.email},
            )
        )
        db.commit()
    return {"revoked": True, "inviteId": invite.id}
