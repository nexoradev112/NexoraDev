#!/bin/sh
set -eu
umask 077

if [ -z "${REDIS_PASSWORD:-}" ]; then
  echo "REDIS_PASSWORD is required" >&2
  exit 1
fi

{
  echo "bind 0.0.0.0"
  echo "protected-mode yes"
  echo "port 6379"
  echo "save \"\""
  echo "appendonly no"
  printf 'requirepass %s\n' "$REDIS_PASSWORD"
} > /tmp/redis.conf

unset REDIS_PASSWORD
exec redis-server /tmp/redis.conf
