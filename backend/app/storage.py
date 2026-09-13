from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .models import StoredFile, Workspace

CATEGORY_CONTENT_TYPES: dict[str, set[str]] = {
    "knowledge": {
        "application/pdf",
        "application/json",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/csv",
        "text/plain",
    },
    "recordings": {"audio/mpeg", "audio/mp4", "audio/ogg", "audio/wav", "audio/webm", "audio/x-wav"},
    "cms": {"image/gif", "image/jpeg", "image/png", "image/webp"},
}
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]+")
FIVE_MIB = 5 * 1024 * 1024
_UPLOAD_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class SavedUpload:
    storage_key: str
    filename: str
    content_type: str
    size: int
    sha256: str


def _assert_free_space(path: Path, minimum_free_bytes: int, incoming_bytes: int = 0) -> None:
    try:
        available = shutil.disk_usage(path).free
    except OSError as exc:
        raise HTTPException(status_code=507, detail="Storage capacity cannot be verified") from exc
    if available - incoming_bytes < minimum_free_bytes:
        raise HTTPException(status_code=507, detail="Server storage reserve would be exceeded")


def safe_filename(value: str) -> str:
    name = Path(value or "upload.bin").name
    name = SAFE_FILENAME.sub("_", name).strip(" .")[:180]
    return name or "upload.bin"


def category_directory(settings: Settings, category: str, workspace_id: int) -> tuple[Path, Path]:
    if category not in CATEGORY_CONTENT_TYPES or workspace_id < 0:
        raise HTTPException(status_code=400, detail="Storage category is invalid")
    configured_root = settings.FILE_STORAGE_ROOT
    configured_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    if configured_root.is_symlink():
        raise HTTPException(status_code=500, detail="Storage root cannot be a symlink")
    root = configured_root.resolve(strict=True)
    category_path = root / category
    category_path.mkdir(exist_ok=True, mode=0o750)
    target = category_path / f"w{workspace_id}"
    target.mkdir(exist_ok=True, mode=0o750)
    for component in (category_path, target):
        try:
            info = component.lstat()
        except OSError as exc:
            raise HTTPException(status_code=500, detail="Storage namespace is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise HTTPException(status_code=500, detail="Storage namespace is invalid")
    return root, target


async def _save_upload_unlocked(
    upload: UploadFile,
    category: str,
    workspace_id: int,
    settings: Settings,
) -> SavedUpload:
    content_type = (upload.content_type or "application/octet-stream").split(";", 1)[0].lower()
    if content_type not in CATEGORY_CONTENT_TYPES.get(category, set()):
        raise HTTPException(status_code=415, detail="File type is not allowed")
    original = safe_filename(upload.filename or "upload.bin")
    suffix = Path(original).suffix.lower()[:12]
    root, directory = category_directory(settings, category, workspace_id)
    _assert_free_space(directory, settings.MIN_STORAGE_FREE_BYTES)
    generated = f"{uuid.uuid4().hex}{suffix}"
    destination = directory / generated
    relative = destination.relative_to(root).as_posix()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    digest = hashlib.sha256()
    size = 0
    prefix = bytearray()
    category_limit = (
        min(settings.MAX_UPLOAD_BYTES, FIVE_MIB)
        if category in {"recordings", "cms"}
        else settings.MAX_UPLOAD_BYTES
    )
    try:
        descriptor = os.open(destination, flags, 0o640)
        with os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > category_limit:
                    raise HTTPException(status_code=413, detail="File exceeds the upload limit")
                _assert_free_space(directory, settings.MIN_STORAGE_FREE_BYTES, len(chunk))
                if len(prefix) < 64:
                    prefix.extend(chunk[: 64 - len(prefix)])
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        _assert_free_space(directory, settings.MIN_STORAGE_FREE_BYTES)
    except BaseException:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        try:
            await upload.close()
        except BaseException:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    if size < 1:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="Empty files are not allowed")
    if category in {"recordings", "cms"} and not _matches_signature(content_type, bytes(prefix)):
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=415, detail="File content does not match its declared type")
    return SavedUpload(relative, original, content_type, size, digest.hexdigest())


async def save_upload(
    upload: UploadFile,
    category: str,
    workspace_id: int,
    settings: Settings,
) -> SavedUpload:
    """Serialize local-disk writes so concurrent tenants cannot race the free-space reserve."""

    async with _UPLOAD_LOCK:
        return await _save_upload_unlocked(upload, category, workspace_id, settings)


def enforce_workspace_storage_quota(
    db: Session,
    workspace_id: int,
    additional_bytes: int,
    settings: Settings,
) -> int:
    """Serialize and enforce the configured local-disk quota for one tenant."""

    if additional_bytes < 0:
        raise ValueError("Storage reservation cannot be negative")
    workspace = db.scalar(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    used = (
        db.scalar(
            select(func.coalesce(func.sum(StoredFile.size), 0)).where(
                StoredFile.workspace_id == workspace_id,
                StoredFile.status == "ready",
            )
        )
        or 0
    )
    if used + additional_bytes > settings.MAX_WORKSPACE_STORAGE_BYTES:
        raise HTTPException(status_code=507, detail="Workspace storage quota exceeded")
    return int(used)


def discard_saved_upload(settings: Settings, saved: SavedUpload) -> None:
    """Best-effort cleanup for a trusted, generated storage key."""

    try:
        resolve_storage_key(settings, saved.storage_key).unlink(missing_ok=True)
    except (HTTPException, OSError):
        # A namespace integrity error is safer to leave for an operator than to
        # follow a swapped path during rollback cleanup.
        return


def _matches_signature(content_type: str, prefix: bytes) -> bool:
    checks = {
        "image/png": lambda value: value.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": lambda value: value.startswith(b"\xff\xd8\xff"),
        "image/gif": lambda value: value.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": lambda value: value.startswith(b"RIFF") and value[8:12] == b"WEBP",
        "audio/wav": lambda value: value.startswith(b"RIFF") and value[8:12] == b"WAVE",
        "audio/x-wav": lambda value: value.startswith(b"RIFF") and value[8:12] == b"WAVE",
        "audio/ogg": lambda value: value.startswith(b"OggS"),
        "audio/webm": lambda value: value.startswith(b"\x1aE\xdf\xa3"),
        "audio/mp4": lambda value: len(value) >= 12 and value[4:8] == b"ftyp",
        "audio/mpeg": lambda value: (
            value.startswith(b"ID3") or (len(value) >= 2 and value[0] == 0xFF and value[1] & 0xE0 == 0xE0)
        ),
    }
    check = checks.get(content_type)
    return bool(check and check(prefix))


def resolve_storage_key(settings: Settings, storage_key: str) -> Path:
    root_path = settings.FILE_STORAGE_ROOT
    if root_path.is_symlink():
        raise HTTPException(status_code=404, detail="File is unavailable")
    root = root_path.resolve(strict=True)
    relative = Path(storage_key)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise HTTPException(status_code=404, detail="File is unavailable")
    candidate = root
    for index, part in enumerate(relative.parts):
        candidate = candidate / part
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise HTTPException(status_code=404, detail="File is unavailable") from exc
        if stat.S_ISLNK(info.st_mode):
            raise HTTPException(status_code=404, detail="File is unavailable")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise HTTPException(status_code=404, detail="File is unavailable")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root) or not stat.S_ISREG(resolved.stat().st_mode):
        raise HTTPException(status_code=404, detail="File is unavailable")
    return resolved
