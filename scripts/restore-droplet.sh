#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo_root"

backup_dir=${1:-}
confirmation=${2:-}

if [[ -z $backup_dir || $confirmation != --confirm-destructive-restore ]]; then
  echo "Usage: sudo scripts/restore-droplet.sh /absolute/backup/directory --confirm-destructive-restore" >&2
  exit 2
fi
if [[ $backup_dir != /* || $backup_dir == / || ! -d $backup_dir ]]; then
  echo "Backup directory must be an existing absolute path other than /." >&2
  exit 2
fi
for required_file in postgres.dump files.tar.gz voice-spool.tar.gz secrets.tar.gz SHA256SUMS; do
  if [[ ! -s $backup_dir/$required_file ]]; then
    echo "Missing backup file: $required_file" >&2
    exit 1
  fi
done

(
  cd "$backup_dir"
  sha256sum --check SHA256SUMS
)

# Refuse traversal, links, and device entries before any destructive action.
# The backup format produced by this repository needs only directories and
# regular files.
python3 - "$backup_dir/files.tar.gz" "$backup_dir/voice-spool.tar.gz" <<'PY'
from pathlib import PurePosixPath
import sys
import tarfile

for archive_name in sys.argv[1:]:
    with tarfile.open(archive_name, mode="r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise SystemExit(f"Unsafe archive member in {archive_name}")
            if not (member.isdir() or member.isfile()):
                raise SystemExit(f"Unsupported archive member type in {archive_name}")
PY

pre_restore_root=/var/backups/nexora-pre-restore
"$repo_root/scripts/backup-droplet.sh" "$pre_restore_root"

docker compose stop nginx web voice-agent dispatcher api

printf '%s\n' \
  'DROP SCHEMA public CASCADE;' \
  "SELECT format('CREATE SCHEMA public AUTHORIZATION %I', :'app_user')" \
  '\gexec' \
  | docker compose exec -T db sh -eu -c \
      'PGPASSWORD="$POSTGRES_PASSWORD" psql --set ON_ERROR_STOP=1 --set=app_user="$APP_DB_USER" --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"'

docker compose exec -T db sh -eu -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --no-owner --role="$APP_DB_USER" --exit-on-error' \
  < "$backup_dir/postgres.dump"

docker compose run --rm --no-deps --entrypoint sh api -eu -c \
  'find /var/lib/nexora/files -mindepth 1 -delete; tar -C /var/lib/nexora/files -xzf -' \
  < "$backup_dir/files.tar.gz"

docker compose run --rm --no-deps --entrypoint sh voice-agent -eu -c \
  'find /var/lib/nexora/voice-spool -mindepth 1 -delete; tar -C /var/lib/nexora/voice-spool -xzf -' \
  < "$backup_dir/voice-spool.tar.gz"

docker compose up -d
echo "Database and files restored. A pre-restore backup was saved under $pre_restore_root."
