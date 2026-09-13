from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

from .config import Settings
from .models import Integration


def _master_key(settings: Settings) -> bytes:
    try:
        key = base64.b64decode(settings.CREDENTIAL_MASTER_KEY, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="Credential vault is unavailable") from exc
    if len(key) != 32:
        raise HTTPException(status_code=503, detail="Credential vault is unavailable")
    return key


def _aad(integration: Integration) -> bytes:
    if not integration.id:
        raise ValueError("Integration record must be flushed before encryption")
    return (
        f"integration-credential|w={integration.workspace_id}|record={integration.id}"
        f"|v={integration.key_version}"
    ).encode()


def encrypt_integration_secret(integration: Integration, secret: str, settings: Settings) -> None:
    if not secret or len(secret) > 16_384:
        raise HTTPException(status_code=422, detail="Integration credential must contain 1-16384 characters")
    nonce = os.urandom(12)
    encrypted = AESGCM(_master_key(settings)).encrypt(nonce, secret.encode(), _aad(integration))
    integration.secret_nonce = base64.b64encode(nonce).decode()
    integration.encrypted_secret = base64.b64encode(encrypted).decode()


def decrypt_integration_secret(integration: Integration, settings: Settings) -> str:
    if not integration.encrypted_secret or not integration.secret_nonce:
        raise HTTPException(status_code=503, detail="Integration credential is not configured")
    try:
        nonce = base64.b64decode(integration.secret_nonce, validate=True)
        encrypted = base64.b64decode(integration.encrypted_secret, validate=True)
        return AESGCM(_master_key(settings)).decrypt(nonce, encrypted, _aad(integration)).decode()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503, detail="Stored integration credential cannot be decrypted"
        ) from exc
