from __future__ import annotations

import argparse
import base64
import getpass
import os
import re
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def create_superadmin(email: str, name: str) -> None:
    from sqlalchemy import select

    from .db import SessionLocal
    from .models import User
    from .security import hash_password, normalize_email, now_utc

    email = normalize_email(email)
    password = getpass.getpass("Superadmin password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    with SessionLocal.begin() as db:
        existing = db.scalar(select(User).where(User.email == email))
        if existing:
            existing.is_superadmin = True
            existing.status = "active"
            existing.password_hash = hash_password(password)
            existing.password_changed_at = now_utc()
            existing.name = name
        else:
            db.add(
                User(
                    email=email,
                    name=name,
                    password_hash=hash_password(password),
                    is_superadmin=True,
                )
            )
    print(f"Superadmin ready: {email}")


def create_tenant_admin(email: str, name: str, workspace_name: str) -> None:
    """Bootstrap an unlicensed tenant without opening public registration."""

    from sqlalchemy import select

    from .db import SessionLocal
    from .models import Membership, User, Workspace
    from .security import hash_password, normalize_email

    email = normalize_email(email)
    password = getpass.getpass("Tenant owner password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    base = re.sub(r"[^a-z0-9]+", "-", workspace_name.lower()).strip("-")[:70] or "workspace"
    with SessionLocal.begin() as db:
        if db.scalar(select(User.id).where(User.email == email)):
            raise SystemExit("A user with that email already exists")
        slug = base
        while db.scalar(select(Workspace.id).where(Workspace.slug == slug)):
            slug = f"{base[:60]}-{secrets.token_hex(3)}"
        user = User(email=email, name=name, password_hash=hash_password(password))
        workspace = Workspace(name=workspace_name.strip(), slug=slug, status="pending_license")
        db.add_all([user, workspace])
        db.flush()
        workspace_id = workspace.id
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
    print(f"Tenant owner ready: {email}; workspace_id={workspace_id}")


def generate_secrets() -> None:
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    values = {
        "CREDENTIAL_MASTER_KEY": base64.b64encode(os.urandom(32)).decode(),
        "PHONE_HASH_KEY": base64.b64encode(os.urandom(32)).decode(),
        "WORKER_CONFIG_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="),
        "CALL_WORKER_TOKEN": base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("="),
        "LICENSE_SIGNING_PRIVATE_KEY": base64.b64encode(private_raw).decode(),
        "LICENSE_SIGNING_PUBLIC_KEY": base64.b64encode(public_raw).decode(),
    }
    print("\n".join(f"{key}={value}" for key, value in values.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Nexora control-plane administration")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Create the initial schema (use Alembic for upgrades)")
    create = sub.add_parser("create-superadmin", help="Create or promote the platform operator")
    create.add_argument("--email", required=True)
    create.add_argument("--name", default="Platform Administrator")
    tenant = sub.add_parser("create-tenant-admin", help="Create an unlicensed tenant and owner")
    tenant.add_argument("--email", required=True)
    tenant.add_argument("--name", default="Tenant Owner")
    tenant.add_argument("--workspace", required=True)
    sub.add_parser("generate-secrets", help="Generate deployment secrets and Ed25519 keys")
    args = parser.parse_args()
    if args.command == "migrate":
        # Import models so every mapped table is registered before create_all.
        from . import models  # noqa: F401
        from .db import Base, engine

        Base.metadata.create_all(engine)
        print("Database schema is ready")
    elif args.command == "create-superadmin":
        create_superadmin(args.email, args.name)
    elif args.command == "create-tenant-admin":
        create_tenant_admin(args.email, args.name, args.workspace)
    elif args.command == "generate-secrets":
        generate_secrets()


if __name__ == "__main__":
    main()
