from __future__ import annotations

import asyncio
import re
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import (
    agents,
    auth,
    chat,
    cms,
    dashboard,
    files,
    integrations,
    licenses,
    providers,
    voice,
    workspaces,
)
from .config import get_settings
from .db import SessionLocal
from .postcall import run_pending_post_calls
from .room_termination import run_pending_room_terminations
from .security import verify_worker_request

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run the durable post-call outbox inside the one API writer process."""

    if settings.ENVIRONMENT == "test":
        yield
        return
    stopped = asyncio.Event()

    async def poll_post_call_outbox() -> None:
        while not stopped.is_set():
            try:
                await asyncio.to_thread(run_pending_post_calls, settings, 20)
            except Exception:  # noqa: BLE001
                # Jobs stay leased/pending in PostgreSQL and are retried; never
                # leak payloads or credentials into process logs here.
                await asyncio.sleep(0)
            try:
                await asyncio.wait_for(stopped.wait(), timeout=15)
            except TimeoutError:
                pass

    async def poll_room_termination_outbox() -> None:
        while not stopped.is_set():
            try:
                await asyncio.to_thread(run_pending_room_terminations, settings, 20)
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0)
            try:
                await asyncio.wait_for(stopped.wait(), timeout=5)
            except TimeoutError:
                pass

    post_call_task = asyncio.create_task(poll_post_call_outbox(), name="post-call-outbox")
    termination_task = asyncio.create_task(poll_room_termination_outbox(), name="room-termination-outbox")
    try:
        yield
    finally:
        stopped.set()
        await asyncio.gather(post_call_task, termination_task)


app = FastAPI(
    title="Nexora Control Plane",
    version="0.1.0",
    docs_url=None if settings.ENVIRONMENT == "production" else "/docs",
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(settings.allowed_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Workspace-Id", "X-Request-Id"],
)


@app.middleware("http")
async def security_boundary(request: Request, call_next):
    request_id = request.headers.get("x-request-id", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
        request_id = secrets.token_hex(12)
    request.state.request_id = request_id
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            limit = settings.MAX_UPLOAD_BYTES + 1024 * 1024
            if int(content_length) > limit:
                return JSONResponse(
                    {"error": "Request body is too large", "requestId": request_id}, status_code=413
                )
        except ValueError:
            return JSONResponse({"error": "Invalid Content-Length", "requestId": request_id}, status_code=400)
    if request.url.path.startswith("/api/internal/"):
        try:
            body = await request.body()
            with SessionLocal() as worker_db:
                verify_worker_request(worker_db, request, body, settings)
        except StarletteHTTPException as exc:
            message = exc.detail if isinstance(exc.detail, str) else "Worker authentication failed"
            return JSONResponse({"error": message, "requestId": request_id}, status_code=exc.status_code)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        is_worker = request.url.path.startswith("/api/internal/") and getattr(
            request.state, "worker_authenticated", False
        )
        if not is_worker:
            origin = request.headers.get("origin", "").rstrip("/")
            if not origin or origin not in settings.allowed_origins:
                return JSONResponse(
                    {"error": "Request origin is not allowed", "requestId": request_id},
                    status_code=403,
                )
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), payment=()"
    response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store")
    return response


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return JSONResponse(
        {"error": message, "requestId": getattr(request.state, "request_id", None)},
        status_code=exc.status_code,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(item) for item in first.get("loc", [])[1:])
    message = str(first.get("msg", "Invalid request"))
    if location:
        message = f"{location}: {message}"
    return JSONResponse(
        {"error": message, "requestId": getattr(request.state, "request_id", None)},
        status_code=422,
    )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, _exc: Exception) -> JSONResponse:
    # Details stay in structured server logs; never reflect provider responses,
    # SQL, credentials, or filesystem paths to clients.
    return JSONResponse(
        {"error": "Internal server error", "requestId": getattr(request.state, "request_id", None)},
        status_code=500,
    )


@app.get("/health/live", include_in_schema=False)
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
def health_ready() -> dict[str, str]:
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ready"}


for api_router in (
    auth.router,
    workspaces.router,
    licenses.router,
    providers.router,
    integrations.router,
    agents.router,
    chat.router,
    files.router,
    cms.router,
    voice.router,
    dashboard.router,
):
    app.include_router(api_router)


'''
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
'''