# Local PostgreSQL

This is the database used by the FastAPI control plane on a developer machine.
Production PostgreSQL is different: see [DROPLET-DEPLOYMENT.md](DROPLET-DEPLOYMENT.md).
Do not reuse Skyvern, warehouse-tester, or any other project's Postgres container.

## Local credentials

These match `backend/.env`. They are for local development only.

| Field | Value |
| --- | --- |
| Container | `nexora-postgres` |
| Image | `postgres:16` |
| Host | `127.0.0.1` |
| Port | `5432` |
| Database | `nexora` |
| User | `nexora` |
| Password | `nexora` |
| SQLAlchemy URL | `postgresql+psycopg://nexora:nexora@127.0.0.1:5432/nexora` |
| `psql` URL | `postgresql://nexora:nexora@127.0.0.1:5432/nexora` |

GUI tools (DBeaver, pgAdmin, TablePlus, VS Code Postgres) use the same host,
port, database, user, and password.

`backend/.env` is loaded only when you run commands from the `backend/`
directory. Keep `DATABASE_URL` in that file; do not put it in `NEXT_PUBLIC_*`
or `VITE_*` variables.

## Start, stop, status

Create the container once (uses the local `postgres:16` image):

```powershell
docker run -d --name nexora-postgres --restart unless-stopped `
  -e POSTGRES_USER=nexora `
  -e POSTGRES_PASSWORD=nexora `
  -e POSTGRES_DB=nexora `
  -p 5432:5432 `
  postgres:16
```

After that:

```powershell
docker start nexora-postgres
docker stop nexora-postgres
docker ps --filter "name=nexora-postgres"
docker exec nexora-postgres pg_isready -U nexora -d nexora
```

If `docker run` fails because the name already exists, use `docker start`
instead. If port `5432` is already taken, stop the other container first.
Do not point this app at `skyvern-postgres-1` or `wh_tester_api-db-1`.

## Schema bootstrap

From `backend/`, using the project venv:

```powershell
cd backend
.\.venv\Scripts\python.exe -m app.cli migrate
```

That runs SQLAlchemy `create_all`. It creates missing tables and is safe to
repeat. It does not alter existing columns. Schema-changing upgrades should
use Alembic when those migrations exist.

Equivalent if `nexora-admin` is on PATH:

```powershell
nexora-admin migrate
```

## App CLI (writes through SQLAlchemy)

Always run these from `backend/` so `.env` is loaded. Passwords are prompted;
do not put them on the command line.

```powershell
cd backend
.\.venv\Scripts\python.exe -m app.cli migrate
.\.venv\Scripts\python.exe -m app.cli create-superadmin --email owner@example.com
.\.venv\Scripts\python.exe -m app.cli create-tenant-admin --email tenant@example.com --workspace "Demo Workspace"
.\.venv\Scripts\python.exe -m app.cli generate-secrets
```

`create-superadmin` creates or promotes the platform operator. `create-tenant-admin`
creates a user, a `pending_license` workspace, and an owner membership.

## Open a SQL shell

No local `psql` install is required. Use the container:

```powershell
docker exec -it nexora-postgres psql -U nexora -d nexora
```

Useful `psql` commands once you are in:

```sql
\dt
\d users
\q
```

One-shot queries from PowerShell:

```powershell
docker exec -it nexora-postgres psql -U nexora -d nexora -c "\dt"
docker exec -it nexora-postgres psql -U nexora -d nexora -c "SELECT id, email, is_superadmin, status FROM users;"
docker exec -it nexora-postgres psql -U nexora -d nexora -c "SELECT id, name, slug, status FROM workspaces;"
```

## Typical inspection queries

```sql
-- Accounts and tenancy
SELECT id, email, name, is_superadmin, status FROM users;
SELECT id, name, slug, status FROM workspaces;
SELECT m.id, w.slug, u.email, m.role
FROM memberships m
JOIN workspaces w ON w.id = m.workspace_id
JOIN users u ON u.id = m.user_id;

-- Licenses (do not dump signed_payload / signature unless you need them)
SELECT id, workspace_id, plan, seats, status, provider_mode, valid_from, valid_until
FROM licenses;

-- Agents and recent calls
SELECT id, workspace_id, name, channel, status, locale FROM agents;
SELECT id, workspace_id, agent_id, room_name, status, direction, started_at
FROM call_sessions
ORDER BY id DESC
LIMIT 20;
```

## Changing data

Prefer the app CLI or the running API for users, licenses, and credentials.
Direct SQL is for inspection and local cleanup.

Examples of local-only SQL (do not run these on production without a backup):

```sql
-- Look up a user
SELECT id, email, is_superadmin, status FROM users WHERE email = 'owner@example.com';

-- Clear sessions for a user (forces re-login)
DELETE FROM user_sessions WHERE user_id = 1;

-- Reset a workspace back to unlicensed (local only)
UPDATE workspaces SET status = 'pending_license' WHERE id = 1;
```

Do not edit `password_hash`, `encrypted_secret`, license signatures, or
`phone_hash` by hand. Those are produced by the control plane.

## Dump and reset locally

Dump:

```powershell
docker exec nexora-postgres pg_dump -U nexora -d nexora > nexora-local.dump.sql
```

Drop and recreate the public schema, then bootstrap again:

```powershell
docker exec -it nexora-postgres psql -U nexora -d nexora -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
cd backend
.\.venv\Scripts\python.exe -m app.cli migrate
```

Restore a dump:

```powershell
Get-Content nexora-local.dump.sql | docker exec -i nexora-postgres psql -U nexora -d nexora
```

## Tables

Defined in `backend/app/models.py`:

| Table | Purpose |
| --- | --- |
| `users` | Accounts, superadmin flag, password hash |
| `workspaces` | Tenants |
| `memberships` | User-to-workspace roles |
| `user_sessions` | Hashed session cookies |
| `licenses` | Signed entitlements |
| `workspace_invites` | Email invites |
| `provider_connections` | Encrypted BYOK keys |
| `integrations` | HTTP integrations |
| `agents` | Agent graphs and settings |
| `agent_versions` | Saved graph versions |
| `stored_files` | Tenant file metadata |
| `cms_settings` | CMS key/value |
| `cms_media` | CMS media metadata |
| `call_sessions` | Voice/chat call records |
| `post_call_results` | Post-call outbox |
| `livekit_room_termination_jobs` | Room stop retries |
| `phone_numbers` | Telephony inventory |
| `usage_events` / `usage_counters` | Metering |
| `consent_records` / `suppression_entries` | Consent and DNC |
| `audit_logs` | Operator actions |
| `worker_request_nonces` | Worker replay protection |

## Production (do not use local credentials)

On the droplet, Compose starts `postgres:16-alpine` as service `db`. Port
`5432` is not published. The API uses `nexora_app`; the owner role is
`nexora_owner`. Passwords come from the host `.env` created by
`scripts/init-droplet.sh`.

```bash
docker compose exec db psql -U nexora_owner -d nexora
docker compose run --rm api python /usr/local/lib/nexora/migrate-with-lock.py
docker compose run --rm api python -m app.cli create-superadmin --email owner@example.com
sudo ./scripts/backup-droplet.sh /var/backups/nexora
```
