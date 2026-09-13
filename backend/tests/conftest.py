from __future__ import annotations

import base64
import os
import tempfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

TEST_ROOT = tempfile.mkdtemp(prefix="nexora-pytest-")
PRIVATE = Ed25519PrivateKey.generate()
PRIVATE_RAW = PRIVATE.private_bytes(
    serialization.Encoding.Raw,
    serialization.PrivateFormat.Raw,
    serialization.NoEncryption(),
)
PUBLIC_RAW = PRIVATE.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
WORKER_KEY = os.urandom(32)
os.environ.update(
    ENVIRONMENT="test",
    DATABASE_URL=f"sqlite+pysqlite:///{TEST_ROOT}/test.sqlite",
    PUBLIC_BASE_URL="http://testserver",
    ALLOWED_ORIGINS="http://testserver",
    SESSION_COOKIE_SECURE="false",
    ALLOW_PUBLIC_TENANT_REGISTRATION="true",
    FILE_STORAGE_ROOT=f"{TEST_ROOT}/files",
    CREDENTIAL_MASTER_KEY=base64.b64encode(os.urandom(32)).decode(),
    PHONE_HASH_KEY=base64.b64encode(os.urandom(32)).decode(),
    WORKER_CONFIG_KEY=base64.urlsafe_b64encode(WORKER_KEY).decode().rstrip("="),
    CALL_WORKER_TOKEN="worker-token-" + "x" * 40,
    LICENSE_SIGNING_PRIVATE_KEY=base64.b64encode(PRIVATE_RAW).decode(),
    LICENSE_SIGNING_PUBLIC_KEY=base64.b64encode(PUBLIC_RAW).decode(),
    OPENAI_API_KEY="platform-openai-fake-key",
    DEEPGRAM_API_KEY="platform-deepgram-fake-key",
    ELEVENLABS_API_KEY="platform-elevenlabs-fake-key",
    LIVEKIT_INTERNAL_URL="ws://livekit:7880",
    LIVEKIT_PUBLIC_URL="ws://voice.test",
    LIVEKIT_API_KEY="devkey-with-32-characters-000000",
    LIVEKIT_API_SECRET="devsecret-with-32-characters-0000",  # noqa: S106
)

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.security import hash_password  # noqa: E402


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        db.add(
            User(
                email="root@example.com",
                name="Root",
                password_hash=hash_password("Admin-password-123!"),
                is_superadmin=True,
            )
        )
    yield


@pytest.fixture
def origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


@pytest.fixture
def tenant() -> TestClient:
    return TestClient(app)


@pytest.fixture
def superadmin(origin: dict[str, str]) -> TestClient:
    client = TestClient(app)
    response = client.post(
        "/api/auth/login",
        headers=origin,
        json={"email": "root@example.com", "password": "Admin-password-123!"},
    )
    assert response.status_code == 200
    return client
