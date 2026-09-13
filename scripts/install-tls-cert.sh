#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo so certificate files can be copied with restricted ownership." >&2
  exit 1
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo_root"

certificate=${1:-}
private_key=${2:-}
if [[ ! -f $certificate || ! -f $private_key ]]; then
  echo "Usage: sudo scripts/install-tls-cert.sh /path/fullchain.pem /path/privkey.pem" >&2
  exit 2
fi
if [[ ! -f .env ]]; then
  echo ".env is missing; run init-droplet.sh first." >&2
  exit 1
fi

read -r app_uid app_gid < <(python3 - <<'PY'
from pathlib import Path

values = {}
for raw_line in Path('.env').read_text(encoding='utf-8').splitlines():
    line = raw_line.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    key, value = line.split('=', 1)
    if key in {'APP_UID', 'APP_GID'}:
        values[key] = value
print(values.get('APP_UID', ''), values.get('APP_GID', ''))
PY
)

if [[ ! $app_uid =~ ^[0-9]+$ || ! $app_gid =~ ^[0-9]+$ ]]; then
  echo "APP_UID/APP_GID in .env are invalid." >&2
  exit 1
fi

install -d -m 0700 -o "$app_uid" -g "$app_gid" deploy/tls
install -m 0644 -o "$app_uid" -g "$app_gid" "$certificate" deploy/tls/fullchain.pem.new
install -m 0600 -o "$app_uid" -g "$app_gid" "$private_key" deploy/tls/privkey.pem.new
mv -f deploy/tls/fullchain.pem.new deploy/tls/fullchain.pem
mv -f deploy/tls/privkey.pem.new deploy/tls/privkey.pem

if docker compose ps --status running --services | grep -qx nginx; then
  docker compose exec -T nginx nginx -s reload
fi

echo "TLS certificate installed and nginx reloaded when it was running."
