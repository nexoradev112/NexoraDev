from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from contextlib import suppress

from fastapi import HTTPException

from .config import Settings
from .livekit_urls import exact_livekit_origin

SAFE_ROOM_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
ROOM_DELETE_TIMEOUT_SECONDS = 3
ROOM_DELETE_CONCURRENCY = 20


def _livekit_http_origin(settings: Settings) -> str | None:
    if not (settings.LIVEKIT_INTERNAL_URL and settings.LIVEKIT_API_KEY and settings.LIVEKIT_API_SECRET):
        return None
    try:
        origin = exact_livekit_origin(
            settings.LIVEKIT_INTERNAL_URL,
            allowed_schemes={"http", "https", "ws", "wss"},
            error_detail="LiveKit service URL is misconfigured",
        )
    except HTTPException:
        return None
    scheme, authority = origin.split("://", 1)
    return f"{'https' if scheme in {'https', 'wss'} else 'http'}://{authority}"


async def terminate_livekit_room(room_name: str, settings: Settings) -> None:
    """Delete one room or raise so the durable outbox can retry."""

    if not isinstance(room_name, str) or not SAFE_ROOM_NAME.fullmatch(room_name):
        raise ValueError("Invalid LiveKit room name")
    http_origin = _livekit_http_origin(settings)
    if not http_origin:
        raise RuntimeError("LiveKit administration is not configured")
    from livekit import api

    client = api.LiveKitAPI(http_origin, settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
    try:
        await asyncio.wait_for(
            client.room.delete_room(api.DeleteRoomRequest(room=room_name)),
            timeout=ROOM_DELETE_TIMEOUT_SECONDS,
        )
    finally:
        with suppress(Exception):
            await client.aclose()


async def terminate_livekit_rooms(room_names: Sequence[str], settings: Settings) -> None:
    """Compatibility helper; callers needing guarantees use the durable outbox."""

    rooms = tuple(
        dict.fromkeys(
            room_name
            for room_name in room_names
            if isinstance(room_name, str) and SAFE_ROOM_NAME.fullmatch(room_name)
        )
    )
    if not rooms:
        return
    semaphore = asyncio.Semaphore(ROOM_DELETE_CONCURRENCY)

    async def delete_one(room_name: str) -> None:
        async with semaphore:
            try:
                await terminate_livekit_room(room_name, settings)
            except Exception:  # noqa: BLE001
                return

    await asyncio.gather(*(delete_one(room_name) for room_name in rooms))
