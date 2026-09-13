#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this initializer with sudo so it can prepare /var/lib and PostgreSQL ownership." >&2
  exit 1
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo_root"

domain=${1:-localhost}
livekit_domain=${2:-voice.${domain}}
data_root=${3:-/var/lib/nexora}

for value in "$domain" "$livekit_domain"; do
  if [[ ! $value =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
    echo "Invalid DNS hostname: $value" >&2
    exit 1
  fi
done

if [[ $data_root != /* ]] || [[ $data_root == / ]]; then
  echo "The data root must be an absolute path other than /." >&2
  exit 1
fi

for command_name in openssl python3 install; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "$command_name is required" >&2
    exit 1
  fi
done

if [[ -e .env ]]; then
  echo ".env already exists. Refusing to overwrite or rotate production secrets." >&2
  exit 1
fi

caller_uid=${SUDO_UID:-10001}
caller_gid=${SUDO_GID:-10001}
postgres_uid=70
postgres_gid=70

install -d -m 0750 -o "$caller_uid" -g "$caller_gid" "$data_root" "$data_root/files"
install -d -m 0700 -o 10002 -g 10002 "$data_root/voice-spool"
install -d -m 0700 -o "$postgres_uid" -g "$postgres_gid" "$data_root/postgres"
install -d -m 0700 -o "$caller_uid" -g "$caller_gid" deploy/tls
install -d -m 0750 -o "$caller_uid" -g "$caller_gid" deploy/acme

key_material_dir=$(mktemp -d)
cleanup_key_material() {
  rm -rf -- "$key_material_dir"
}
trap cleanup_key_material EXIT
openssl genpkey -algorithm ED25519 -out "$key_material_dir/private.pem"
openssl pkey -in "$key_material_dir/private.pem" -outform DER -out "$key_material_dir/private.der"
openssl pkey -in "$key_material_dir/private.pem" -pubout -outform DER -out "$key_material_dir/public.der"

python3 - "$domain" "$livekit_domain" "$data_root" "$caller_uid" "$caller_gid" "$key_material_dir" <<'PY'
from __future__ import annotations

import base64
import os
import pathlib
import secrets
import sys


domain, livekit_domain, data_root, app_uid, app_gid, key_material_dir = sys.argv[1:]
template_path = pathlib.Path(".env.droplet.example")
target_path = pathlib.Path(".env")
template = template_path.read_text(encoding="utf-8")
private_der = (pathlib.Path(key_material_dir) / "private.der").read_bytes()
public_der = (pathlib.Path(key_material_dir) / "public.der").read_bytes()
if len(private_der) < 32 or len(public_der) < 32:
    raise SystemExit("OpenSSL returned invalid Ed25519 key material")
private_raw = private_der[-32:]
public_raw = public_der[-32:]

app_db_password = secrets.token_hex(32)
replacements = {
    "app.example.com": domain,
    "voice.example.com": livekit_domain,
    "__APP_UID__": app_uid,
    "__APP_GID__": app_gid,
    "__DATA_ROOT__": data_root,
    "__POSTGRES_OWNER_PASSWORD__": secrets.token_hex(32),
    "__APP_DB_PASSWORD__": app_db_password,
    "__CREDENTIAL_MASTER_KEY__": base64.b64encode(os.urandom(32)).decode("ascii"),
    "__PHONE_HASH_KEY__": base64.b64encode(os.urandom(32)).decode("ascii"),
    "__WORKER_CONFIG_KEY__": base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("="),
    "__CALL_WORKER_TOKEN__": secrets.token_hex(32),
    "__LICENSE_SIGNING_PRIVATE_KEY__": base64.b64encode(private_raw).decode("ascii"),
    "__LICENSE_SIGNING_PUBLIC_KEY__": base64.b64encode(public_raw).decode("ascii"),
    "__REDIS_PASSWORD__": secrets.token_hex(32),
    "__LIVEKIT_API_KEY__": "LK" + secrets.token_hex(12),
    "__LIVEKIT_API_SECRET__": secrets.token_hex(32),
}
for old, new in replacements.items():
    template = template.replace(old, new)

temporary_path = pathlib.Path(".env.initializing")
temporary_path.write_text(template, encoding="utf-8")
temporary_path.chmod(0o600)
temporary_path.replace(target_path)
PY

chown "$caller_uid:$caller_gid" .env
chmod 0600 .env
trap - EXIT
cleanup_key_material

echo "Initialized .env with raw Ed25519 signing material and prepared data directories."
echo "Set platform provider values if needed, install trusted TLS, then run docker compose up -d --build."
