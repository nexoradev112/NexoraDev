#!/bin/sh
set -eu

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  python /usr/local/lib/nexora/migrate-with-lock.py
fi

exec "$@"
