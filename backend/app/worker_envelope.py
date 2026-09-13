from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

from .config import Settings


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _worker_key(settings: Settings) -> bytes:
    try:
        value = settings.WORKER_CONFIG_KEY
        key = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="Worker configuration encryption is unavailable") from exc
    if len(key) != 32:
        raise HTTPException(status_code=503, detail="Worker configuration encryption is unavailable")
    return key


def seal_runtime_providers(
    room_name: str,
    runtime_providers: dict[str, Any],
    settings: Settings,
) -> dict[str, str | int]:
    issued_at = int(time.time())
    plaintext = json.dumps(
        {
            "roomName": room_name,
            "issuedAt": issued_at,
            "expiresAt": issued_at + 90,
            "runtimeProviders": runtime_providers,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    nonce = os.urandom(12)
    aad = f"nexora-runtime-providers:v1:{room_name}".encode()
    ciphertext = AESGCM(_worker_key(settings)).encrypt(nonce, plaintext, aad)
    return {
        "v": 1,
        "alg": "A256GCM",
        "nonce": _b64url(nonce),
        "ciphertext": _b64url(ciphertext),
    }
