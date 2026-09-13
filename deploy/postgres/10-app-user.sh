#!/bin/sh
set -eu

if [ -z "${APP_DB_USER:-}" ] || [ -z "${APP_DB_PASSWORD:-}" ]; then
  echo "APP_DB_USER and APP_DB_PASSWORD are required" >&2
  exit 1
fi

PGPASSWORD="$POSTGRES_PASSWORD" psql --set ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=app_user="$APP_DB_USER" \
  --set=app_password="$APP_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD %L', :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user')
\gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'app_user')
\gexec
SELECT format('GRANT USAGE, CREATE ON SCHEMA public TO %I', :'app_user')
\gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', :'app_user')
\gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO %I', :'app_user')
\gexec
SQL
