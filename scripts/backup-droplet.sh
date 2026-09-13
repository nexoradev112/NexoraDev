#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo_root"

backup_root=${1:-/var/backups/nexora}
if [[ $backup_root != /* ]] || [[ $backup_root == / ]]; then
  echo "Backup destination must be an absolute path other than /." >&2
  exit 1
fi

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
staging="$backup_root/.partial-$timestamp-$$"
destination="$backup_root/$timestamp"

mkdir -p "$staging"
cleanup() {
  if [[ -d $staging ]]; then
    rm -rf -- "$staging"
  fi
}
trap cleanup EXIT

docker compose exec -T db sh -eu -c \
  'PGPASSWORD="$APP_DB_PASSWORD" pg_dump --username "$APP_DB_USER" --dbname "$POSTGRES_DB" --format=custom --no-owner' \
  > "$staging/postgres.dump"

docker compose exec -T api \
  tar -C /var/lib/nexora/files -czf - . \
  > "$staging/files.tar.gz"

docker compose exec -T voice-agent \
  tar -C /var/lib/nexora/voice-spool -czf - . \
  > "$staging/voice-spool.tar.gz"

# Recovery also needs the credential-vault, worker-envelope, and raw Ed25519
# signing keys from .env. Treat this archive as highly sensitive.
tar -C "$repo_root" -czf "$staging/secrets.tar.gz" .env

(
  cd "$staging"
  sha256sum postgres.dump files.tar.gz voice-spool.tar.gz secrets.tar.gz > SHA256SUMS
)

chmod 0600 "$staging"/*
mv "$staging" "$destination"
trap - EXIT

echo "Backup completed: $destination"
echo "The secrets archive is sensitive; move the whole directory to encrypted off-host storage."
