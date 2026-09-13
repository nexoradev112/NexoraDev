from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings
from .models import User, UserSession, WorkerRequestNonce

PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)
DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash("constant-time-non-account-password")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_AUTH_RATE_LOCK = threading.Lock()
_AUTH_ATTEMPTS: dict[str, list[tuple[str, float]]] = {}
_MAX_RATE_KEYS = 10_000


def now_utc() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 320 or not EMAIL_RE.fullmatch(value):
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    return value


def validate_password(password: str) -> None:
    if len(password) < 12 or len(password) > 256:
        raise HTTPException(status_code=422, detail="Password must contain 12-256 characters")
    classes = sum(
        bool(pattern.search(password))
        for pattern in (
            re.compile(r"[a-z]"),
            re.compile(r"[A-Z]"),
            re.compile(r"\d"),
            re.compile(r"[^A-Za-z0-9]"),
        )
    )
    if classes < 3:
        raise HTTPException(status_code=422, detail="Password must use at least three character classes")


def hash_password(password: str) -> str:
    validate_password(password)
    return PASSWORD_HASHER.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def assert_auth_rate_limit(
    request: Request,
    scope: str,
    identity: str,
    *,
    limit: int,
    window_seconds: int,
) -> tuple[str, str]:
    current = time.monotonic()
    client = request.client.host if request.client else "unknown"
    key = sha256_text(f"{scope}|{client}|{identity}")
    with _AUTH_RATE_LOCK:
        if len(_AUTH_ATTEMPTS) > _MAX_RATE_KEYS:
            oldest = sorted(_AUTH_ATTEMPTS, key=lambda item: _AUTH_ATTEMPTS[item][-1][1])[
                : len(_AUTH_ATTEMPTS) // 10
            ]
            for item in oldest:
                _AUTH_ATTEMPTS.pop(item, None)
        values = [item for item in _AUTH_ATTEMPTS.get(key, []) if item[1] > current - window_seconds]
        if len(values) >= limit:
            retry_after = max(1, round(window_seconds - (current - values[0][1])))
            raise HTTPException(
                status_code=429,
                detail="Too many authentication attempts",
                headers={"Retry-After": str(retry_after)},
            )
        reservation = secrets.token_urlsafe(16)
        values.append((reservation, current))
        _AUTH_ATTEMPTS[key] = values
    return key, reservation


def release_auth_attempt(reservation: tuple[str, str]) -> None:
    """Remove a successful attempt so shared-NAT users do not lock each other out."""

    key, reservation_id = reservation
    with _AUTH_RATE_LOCK:
        values = [item for item in _AUTH_ATTEMPTS.get(key, []) if item[0] != reservation_id]
        if values:
            _AUTH_ATTEMPTS[key] = values
        else:
            _AUTH_ATTEMPTS.pop(key, None)


def create_session(db: Session, user: User, request: Request, settings: Settings) -> str:
    raw = "ses_" + secrets.token_urlsafe(40)
    ua = request.headers.get("user-agent", "")[:500]
    session = UserSession(
        user_id=user.id,
        token_hash=sha256_text(raw),
        expires_at=now_utc() + timedelta(hours=settings.SESSION_TTL_HOURS),
        user_agent_hash=sha256_text(ua) if ua else "",
    )
    db.add(session)
    db.flush()
    return raw


def set_session_cookie(response: Response, raw_token: str, settings: Settings) -> None:
    response.set_cookie(
        settings.SESSION_COOKIE_NAME,
        raw_token,
        max_age=settings.SESSION_TTL_HOURS * 3600,
        httponly=True,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.SESSION_COOKIE_NAME,
        httponly=True,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def authenticate_session(db: Session, request: Request, settings: Settings) -> tuple[User, UserSession]:
    token = request.cookies.get(settings.SESSION_COOKIE_NAME, "")
    if not token or len(token) > 256:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    session = db.scalar(select(UserSession).where(UserSession.token_hash == sha256_text(token)))
    current = now_utc()
    if (
        not session
        or session.revoked_at
        or aware(session.expires_at) <= current
        or aware(session.last_seen_at) + timedelta(hours=settings.SESSION_IDLE_HOURS) <= current
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    user = db.get(User, session.user_id)
    if not user or user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account unavailable")
    if aware(session.created_at) < aware(user.password_changed_at):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    if aware(session.last_seen_at) + timedelta(minutes=5) < current:
        session.last_seen_at = current
        db.commit()
    return user, session


def worker_signature(
    token: str,
    timestamp: int,
    nonce: str,
    method: str,
    path: str,
    body: bytes,
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"v1\n{timestamp}\n{nonce}\n{method.upper()}\n{path}\n{body_hash}".encode()
    return hmac.new(token.encode(), canonical, hashlib.sha256).hexdigest()


def verify_worker_request(
    db: Session,
    request: Request,
    body: bytes,
    settings: Settings,
) -> None:
    if len(settings.CALL_WORKER_TOKEN) < 32:
        raise HTTPException(status_code=401, detail="Invalid worker credentials")
    timestamp_text = request.headers.get("x-worker-timestamp", "")
    nonce = request.headers.get("x-worker-nonce", "")
    supplied_signature = request.headers.get("x-worker-signature", "")
    try:
        timestamp = int(timestamp_text)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid worker request signature") from exc
    current = int(time.time())
    if abs(current - timestamp) > 60:
        raise HTTPException(status_code=401, detail="Worker request timestamp expired")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", nonce) or not re.fullmatch(
        r"[a-f0-9]{64}", supplied_signature
    ):
        raise HTTPException(status_code=401, detail="Invalid worker request signature")
    expected = worker_signature(
        settings.CALL_WORKER_TOKEN,
        timestamp,
        nonce,
        request.method,
        request.url.path,
        body,
    )
    if not hmac.compare_digest(supplied_signature, expected):
        raise HTTPException(status_code=401, detail="Invalid worker request signature")
    expiry = now_utc() + timedelta(minutes=5)
    try:
        db.execute(delete(WorkerRequestNonce).where(WorkerRequestNonce.expires_at <= now_utc()))
        db.add(WorkerRequestNonce(nonce=nonce, request_path=request.url.path, expires_at=expiry))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=401, detail="Worker request replay rejected") from exc
    request.state.worker_authenticated = True


def authenticate_worker(request: Request, settings: Settings) -> None:
    del settings
    if getattr(request.state, "worker_authenticated", False) is not True:
        raise HTTPException(status_code=401, detail="Signed worker request required")
