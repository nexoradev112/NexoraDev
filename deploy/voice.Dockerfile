# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.8.22 AS uv

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/app/voice/.venv/bin:$PATH \
    HOME=/tmp

COPY --from=uv /uv /uvx /bin/

WORKDIR /app/voice

COPY services/livekit-agent/pyproject.toml services/livekit-agent/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY services/livekit-agent/*.py ./

RUN groupadd --gid 10002 voice \
    && useradd --uid 10002 --gid voice --no-create-home voice \
    && chown -R voice:voice /app

USER voice

EXPOSE 8081

HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/', timeout=3).read()"]

ENTRYPOINT ["python"]
CMD ["agent.py", "start"]
