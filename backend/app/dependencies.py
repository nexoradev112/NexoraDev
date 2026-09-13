from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import get_db
from .licensing import LicenseClaims, active_license_for_workspace, require_feature
from .models import Membership, User, UserSession, Workspace
from .security import authenticate_session, aware, now_utc

ROLE_LEVEL = {"viewer": 0, "member": 1, "operator": 2, "admin": 3, "owner": 4}


@dataclass(frozen=True)
class Principal:
    user: User
    session: UserSession


@dataclass(frozen=True)
class WorkspaceAccess:
    user: User
    session: UserSession
    workspace: Workspace
    membership: Membership
    license: LicenseClaims | None


def current_principal(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    user, session = authenticate_session(db, request, settings)
    return Principal(user=user, session=session)


def require_superadmin(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if not principal.user.is_superadmin:
        raise HTTPException(status_code=403, detail="Superadmin access required")
    return principal


def require_recent_auth(principal: Principal, *, minutes: int = 15) -> None:
    if aware(principal.session.created_at) < now_utc() - timedelta(minutes=minutes):
        raise HTTPException(status_code=403, detail="Sign in again before performing this sensitive action")


def select_workspace_access(
    db: Session,
    principal: Principal,
    workspace_selector: str | None,
) -> WorkspaceAccess:
    query = (
        select(Membership, Workspace)
        .join(Workspace, Workspace.id == Membership.workspace_id)
        .where(Membership.user_id == principal.user.id)
    )
    if workspace_selector:
        try:
            workspace_id = int(workspace_selector)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="x-workspace-id must be numeric") from exc
        if workspace_id < 1:
            raise HTTPException(status_code=400, detail="x-workspace-id is invalid")
        row = db.execute(query.where(Membership.workspace_id == workspace_id)).first()
        if not row:
            # Do not reveal whether another tenant owns the selected id.
            raise HTTPException(status_code=404, detail="Workspace not found")
    else:
        rows = db.execute(query.order_by(Membership.workspace_id).limit(2)).all()
        if not rows:
            raise HTTPException(status_code=403, detail="No workspace membership")
        if len(rows) > 1:
            raise HTTPException(status_code=400, detail="Select a workspace with x-workspace-id")
        row = rows[0]
    membership, workspace = row
    if workspace.status == "suspended":
        raise HTTPException(status_code=403, detail="Workspace is suspended")
    return WorkspaceAccess(principal.user, principal.session, workspace, membership, None)


def require_workspace_unlicensed(minimum_role: str = "viewer") -> Callable[..., WorkspaceAccess]:
    if minimum_role not in ROLE_LEVEL:
        raise ValueError("Unknown workspace role")

    def dependency(
        request: Request,
        principal: Annotated[Principal, Depends(current_principal)],
        db: Annotated[Session, Depends(get_db)],
        x_workspace_id: Annotated[str | None, Header()] = None,
        workspace: Annotated[int | None, Query(ge=1)] = None,
    ) -> WorkspaceAccess:
        if x_workspace_id and workspace is not None and x_workspace_id != str(workspace):
            raise HTTPException(status_code=400, detail="Workspace selectors disagree")
        access = select_workspace_access(
            db, principal, x_workspace_id or (str(workspace) if workspace else None)
        )
        if ROLE_LEVEL.get(access.membership.role, -1) < ROLE_LEVEL[minimum_role]:
            raise HTTPException(status_code=403, detail="Insufficient workspace role")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            locked_workspace = db.scalar(
                select(Workspace)
                .where(Workspace.id == access.workspace.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            locked_membership = db.scalar(
                select(Membership)
                .where(
                    Membership.id == access.membership.id,
                    Membership.workspace_id == access.workspace.id,
                    Membership.user_id == principal.user.id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if not locked_workspace or locked_workspace.status == "suspended":
                raise HTTPException(status_code=403, detail="Workspace is suspended")
            if not locked_membership or ROLE_LEVEL.get(locked_membership.role, -1) < ROLE_LEVEL[minimum_role]:
                raise HTTPException(status_code=403, detail="Workspace access changed")
            access = WorkspaceAccess(
                principal.user,
                principal.session,
                locked_workspace,
                locked_membership,
                None,
            )
        return access

    return dependency


def require_workspace(
    minimum_role: str = "viewer", feature: str | None = None
) -> Callable[..., WorkspaceAccess]:
    def dependency(
        request: Request,
        principal: Annotated[Principal, Depends(current_principal)],
        db: Annotated[Session, Depends(get_db)],
        settings: Annotated[Settings, Depends(get_settings)],
        x_workspace_id: Annotated[str | None, Header()] = None,
        workspace: Annotated[int | None, Query(ge=1)] = None,
    ) -> WorkspaceAccess:
        if x_workspace_id and workspace is not None and x_workspace_id != str(workspace):
            raise HTTPException(status_code=400, detail="Workspace selectors disagree")
        access = select_workspace_access(
            db, principal, x_workspace_id or (str(workspace) if workspace else None)
        )
        if ROLE_LEVEL.get(access.membership.role, -1) < ROLE_LEVEL[minimum_role]:
            raise HTTPException(status_code=403, detail="Insufficient workspace role")
        # Every tenant mutation takes the same Workspace -> License lock order as
        # license revoke/activation. This prevents stale entitlement checks from
        # committing after a concurrent revoke or feature downgrade.
        lock = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        locked_workspace = access.workspace
        locked_membership = access.membership
        if lock:
            locked_workspace = db.scalar(
                select(Workspace)
                .where(Workspace.id == access.workspace.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if not locked_workspace or locked_workspace.status == "suspended":
                raise HTTPException(status_code=403, detail="Workspace is suspended")
        claims = active_license_for_workspace(db, access.workspace.id, settings, lock=lock)
        require_feature(claims, feature)
        if lock:
            locked_membership = db.scalar(
                select(Membership)
                .where(
                    Membership.id == access.membership.id,
                    Membership.workspace_id == access.workspace.id,
                    Membership.user_id == principal.user.id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if not locked_membership or ROLE_LEVEL.get(locked_membership.role, -1) < ROLE_LEVEL[minimum_role]:
                raise HTTPException(status_code=403, detail="Workspace access changed")
        return WorkspaceAccess(
            access.user,
            access.session,
            locked_workspace,
            locked_membership,
            claims,
        )

    return dependency
