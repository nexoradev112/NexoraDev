from __future__ import annotations

import re
from urllib.parse import urlparse

from fastapi import HTTPException

UNSAFE_URL_CHARACTER = re.compile(r"[\\\x00-\x20\x7f]")


def exact_livekit_origin(
    value: str,
    *,
    allowed_schemes: set[str],
    error_detail: str,
) -> str:
    """Validate and canonicalize a LiveKit origin without accepting URL suffixes.

    LiveKit credentials are scoped to a service origin. Paths, query strings,
    fragments, userinfo, and malformed ports are rejected so tenant input cannot
    quietly retarget an SDK request to a surprising URL.
    """

    if not isinstance(value, str) or value != value.strip() or UNSAFE_URL_CHARACTER.search(value):
        raise HTTPException(status_code=503, detail=error_detail)
    parsed = urlparse(value)
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
        or "%" in parsed.netloc
    ):
        raise HTTPException(status_code=503, detail=error_detail)
    try:
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=error_detail) from exc
    hostname = parsed.hostname
    if not hostname or hostname.startswith(".") or hostname.endswith("."):
        raise HTTPException(status_code=503, detail=error_detail)
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        rendered_host = f"{rendered_host}:{port}"
    canonical = f"{parsed.scheme}://{rendered_host}"
    if value.lower() not in {canonical.lower(), f"{canonical}/".lower()}:
        raise HTTPException(status_code=503, detail=error_detail)
    return canonical
