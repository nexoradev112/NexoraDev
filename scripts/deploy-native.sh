#!/usr/bin/env bash
# Update the app on the Ubuntu host. PostgreSQL is the database already named
# in backend/.env, which is the same database local development uses.
# create_all adds missing tables and leaves existing rows in place.
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo_root"

ref=${1:?Usage: bash scripts/deploy-native.sh <commit-or-tag>}
case "$ref" in
  *[!A-Za-z0-9._/-]*|"")
    echo "Ref may contain only letters, numbers, and . _ / -" >&2
    exit 2
    ;;
esac

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Server checkout has edits to tracked files. Deploy stopped." >&2
  exit 3
fi

for tool in git node npm uv python3 pg_dump systemctl sudo; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Install $tool on the server before deploying." >&2
    exit 4
  fi
done
if ! sudo -n true; then
  echo "The deploy user needs passwordless sudo so the script can restart services." >&2
  exit 4
fi

test -f backend/.env || {
  echo "Create /opt/nexora/backend/.env on the server. Point DATABASE_URL at the shared PostgreSQL database." >&2
  exit 5
}
test -f services/livekit-agent/.env.local || {
  echo "Create /opt/nexora/services/livekit-agent/.env.local on the server." >&2
  exit 5
}

python3 - <<'PY'
from pathlib import Path

def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values

api = load_env(Path("backend/.env"))
if not api.get("DATABASE_URL", "").startswith("postgresql"):
    raise SystemExit("backend/.env DATABASE_URL must be the shared server PostgreSQL URL")

voice = load_env(Path("services/livekit-agent/.env.local"))
required = (
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "APP_URL",
    "CALL_WORKER_TOKEN",
    "WORKER_CONFIG_KEY",
    "ALLOW_PRIVATE_APP_URL",
)
missing = [key for key in required if not voice.get(key, "").strip()]
if missing:
    raise SystemExit("Voice env is missing: " + ", ".join(missing))
PY

previous=$(git rev-parse HEAD)
git fetch --tags origin
if [[ "$ref" =~ ^[0-9a-f]{7,40}$ ]]; then
  git fetch origin "$ref"
  git checkout --detach FETCH_HEAD
elif git show-ref --verify --quiet "refs/tags/$ref"; then
  git checkout --detach "refs/tags/$ref"
else
  git fetch origin "$ref"
  git checkout --detach FETCH_HEAD
fi

backup_dir="$repo_root/backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"
python3 - "$backup_dir/postgres.dump" <<'PY'
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values

url = load_env(Path("backend/.env"))["DATABASE_URL"]
normalized = url.replace("postgresql+psycopg://", "postgresql://", 1)
parsed = urlparse(normalized)
if not parsed.hostname or not parsed.username or not parsed.path:
    raise SystemExit("DATABASE_URL is missing host, user, or database name")
dump_env = os.environ.copy()
dump_env["PGPASSWORD"] = unquote(parsed.password or "")
subprocess.check_call(
    [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--host",
        parsed.hostname,
        "--port",
        str(parsed.port or 5432),
        "--username",
        parsed.username,
        "--dbname",
        unquote(parsed.path.lstrip("/")),
        "--file",
        sys.argv[1],
    ],
    env=dump_env,
)
PY
chmod 600 "$backup_dir/postgres.dump"
echo "Database backup: $backup_dir/postgres.dump"
echo "Previous commit: $previous"

npm ci
npm run build
mkdir -p .next/standalone/.next
rm -rf .next/standalone/public .next/standalone/.next/static
cp -a public .next/standalone/public
cp -a .next/static .next/standalone/.next/static

(cd backend && uv sync --frozen --no-dev)
(
  cd services/livekit-agent
  uv sync --frozen
  .venv/bin/python agent.py download-files
)

set -a
# shellcheck disable=SC1091
source backend/.env
set +a
(cd backend && .venv/bin/python ../deploy/migrate-with-lock.py)
echo "Schema update finished on the shared database. Existing rows were kept."

run_user=$(id -un)
run_group=$(id -gn)
node_bin=$(command -v node)
unit_dir=$(mktemp -d)
sed -e "s|__RUN_USER__|${run_user}|g" -e "s|__RUN_GROUP__|${run_group}|g" -e "s|__NODE__|${node_bin}|g" \
  deploy/systemd/nexora-api.service > "$unit_dir/nexora-api.service"
sed -e "s|__RUN_USER__|${run_user}|g" -e "s|__RUN_GROUP__|${run_group}|g" -e "s|__NODE__|${node_bin}|g" \
  deploy/systemd/nexora-web.service > "$unit_dir/nexora-web.service"
sed -e "s|__RUN_USER__|${run_user}|g" -e "s|__RUN_GROUP__|${run_group}|g" -e "s|__NODE__|${node_bin}|g" \
  deploy/systemd/nexora-voice.service > "$unit_dir/nexora-voice.service"
sudo cp "$unit_dir"/nexora-api.service "$unit_dir"/nexora-web.service "$unit_dir"/nexora-voice.service \
  /etc/systemd/system/
rm -rf "$unit_dir"
sudo systemctl daemon-reload
sudo systemctl enable nexora-api nexora-web nexora-voice
sudo systemctl stop nexora-api nexora-web nexora-voice || true

free_port() {
  local port="$1"
  local pids
  pids=$(sudo ss -lptn "sport = :${port}" | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | sort -u || true)
  if [[ -n "$pids" ]]; then
    echo "Stopping the previous process on port ${port}"
    sudo kill $pids || true
    sleep 2
  fi
}
free_port 8010
free_port 5008
if pgrep -f "agent.py dev" >/dev/null 2>&1; then
  echo "Stopping the manual voice worker so one worker remains"
  pkill -f "agent.py dev" || true
fi

sudo systemctl restart nexora-api nexora-web nexora-voice
if systemctl is-active --quiet nginx; then
  sudo nginx -t
  sudo systemctl reload nginx
fi

deadline=$((SECONDS + 180))
until curl --fail --silent --show-error http://127.0.0.1:8010/health/ready \
  && curl --fail --silent --show-error -o /dev/null http://127.0.0.1:5008/; do
  if (( SECONDS >= deadline )); then
    echo "App did not become ready. Previous commit was $previous" >&2
    sudo systemctl status nexora-api nexora-web nexora-voice --no-pager || true
    exit 6
  fi
  sleep 5
done

if [[ -n "${NEXORA_PUBLIC_ORIGIN:-}" ]]; then
  curl --fail --silent --show-error -o /dev/null "${NEXORA_PUBLIC_ORIGIN%/}/"
fi

echo "Deployed $(git rev-parse --short HEAD)"
