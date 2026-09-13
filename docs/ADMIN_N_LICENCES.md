There are **no default login credentials**. Nexora never ships a baked-in admin email/password. You create the platform operator yourself, then a tenant workspace stays locked until that operator issues a one-time license key and the tenant activates it.

---

## Admin credentials

Two different “admin” identities exist. Do not mix them.

| Role | What it is | How it is created | Where they sign in |
| --- | --- | --- | --- |
| **Platform superadmin** | `users.is_superadmin = true`. Only this account can issue/revoke licenses and review audio. | CLI only | `/login`, then `/superadmin/licenses` |
| **Tenant owner / admin** | Workspace membership `owner` or `admin`. Can activate a license, add BYOK keys, and invite members. **Cannot** issue or revoke licenses. | Public register, invite, or CLI | `/login`, then `/settings` |

Passwords are Argon2 hashes. There is no password-reset flow in v1. Use at least 12 characters on the register form.

### Create the platform superadmin

Local (from `backend/`, so `.env` loads):

```powershell
cd backend
.\.venv\Scripts\python.exe -m app.cli create-superadmin --email owner@example.com
```

Production droplet:

```bash
docker compose run --rm api \
  python -m app.cli create-superadmin --email owner@example.com
```

The command **prompts** for the password twice. It never takes the password on the command line.

- If that email does not exist, it creates a new superadmin.
- If it already exists, it **promotes** the user, sets `is_superadmin`, resets the password, and marks the account active.

Example placeholder in the docs is `owner@example.com`. Use a real operator address and a password you store in your own credential manager.

Then sign in at `/login` with that email and password. Superadmin-only screens:

- `/superadmin/licenses` — issue, list, revoke licenses; audio review
- `/admin` — CMS

Non-superadmin users who open those URLs get a 404, not a “forbidden” page.

Sensitive license actions require a **session created in the last 15 minutes**. If issue/revoke fails with “Sign in again before performing this sensitive action”, log out and log back in.

### Create a tenant owner

**Path A — public signup** (only if `ALLOW_PUBLIC_TENANT_REGISTRATION=true`; default in code is `false`, local `.env` currently has `true`):

1. Open `/register`.
2. Enter name, workspace name, email, password.
3. The API creates a user, a workspace with status `pending_license`, and an `owner` membership.
4. Browser goes to `/settings?tab=license`. The workspace is locked until a license is activated.

**Path B — CLI, no public signup:**

```powershell
cd backend
.\.venv\Scripts\python.exe -m app.cli create-tenant-admin --email tenant@example.com --workspace "Demo Workspace"
```

That creates the user, a `pending_license` workspace, and an owner membership. Password is prompted interactively. Duplicate emails are rejected.

Look up IDs when you need them:

```sql
SELECT id, email, is_superadmin, status FROM users;
SELECT id, name, slug, status FROM workspaces;
```

Postgres itself is a separate set of credentials (`nexora` / `nexora` locally). That is the database, not the app login.

---

## License flow

End-to-end sequence:

1. Tenant workspace exists (`pending_license`).
2. Superadmin issues a signed license **bound to that workspace ID**.
3. The full key is shown **once**.
4. Tenant owner/admin pastes it on Settings → License and activates.
5. Workspace becomes `active`. Licensed APIs start working.
6. Superadmin can later revoke; the workspace returns to `pending_license`.

Tenant admins cannot issue or revoke licenses.

### 1. Find the workspace ID

The issue form on `/superadmin/licenses` asks for **Workspace ID**, not the name.

Sources:

- Tenant registration API returns `workspaceId`.
- CLI `create-tenant-admin` prints `workspace_id=…`.
- SQL: `SELECT id, name, status FROM workspaces;`
- Superadmin license table later shows issued rows as `Workspace {id}` unless a name is present.

The key is bound to that ID. Activating it in a different workspace returns **403: License belongs to a different workspace**.

### 2. Superadmin issues the license

Go to `/superadmin/licenses` while signed in as superadmin.

Default form values:

| Field | Default |
| --- | --- |
| Plan | `growth` (`starter`, `growth`, `business`, `enterprise`) |
| Seats | `5` |
| Valid from / until | today → +365 days (UTC day boundaries) |
| Provider mode | `hybrid` |
| Hybrid policy | LLM/STT/TTS/telephony = BYOK, **realtime = platform** (locked) |
| Quotas | 36,000 voice seconds, 2,000,000 tokens, 10 agents |
| Features | `agents`, `chat`, `voice` (unless BYOK-only), `providers`, `members`, `analytics`, `recordings`, `telephony`, `post_call` |

Provider modes:

- **BYOK only (chat)** — tenant keys only; voice feature is omitted because this one-worker droplet cannot serve tenant LiveKit.
- **Platform keys only** — platform `.env` keys; tenant cannot add inference secrets.
- **Hybrid** — every kind (`llm`, `stt`, `tts`, `realtime`, `telephony`) must be mapped. Voice licenses **must** map `realtime` to `platform`.

Click **Issue signed license**. The API:

- writes a `licenses` row with status `unused`
- Ed25519-signs the claims
- returns `licenseKey` **only in that HTTP response**
- stores SHA-256 of the key, not the key itself

The UI shows a **SHOWN ONCE** panel. Copy it immediately. Refreshing the page drops it. Closing the panel means you cannot reconstruct the key from the database.

Key shape: `nxlic_v1.<payload>.<signature>.<activation-secret>`.

Deliver the key out of band (secure chat, encrypted email). Do not paste it into tickets or logs.

### 3. Tenant activates the license

Sign in as tenant **owner or admin** (viewers cannot activate).

Go to `/settings` → License. Paste the key and click **Activate license**.

Activation checks:

- Signature matches the platform public key
- Key hash matches the unused row
- `workspace_id` in the payload matches the current workspace
- Dates are currently valid
- Member count ≤ seats
- Status is `unused` (or already `active` — idempotent)
- No other **active** unexpired license exists (otherwise **409**: revoke first)

On first activation:

- license status → `active`
- workspace status → `active`
- audit log `license.activated`

Then the tenant can add allowed provider credentials and use agents/chat/voice according to signed features and quotas.

### 4. After activation

Every licensed tenant API re-verifies the signed claims. Editing seats, dates, features, or status in SQL **fails** integrity checks (HTTP 402).

Typical statuses:

| Status | Meaning |
| --- | --- |
| `unused` | Issued, not yet activated |
| `active` | In force |
| `not_yet_valid` | `validFrom` is in the future |
| `expired` | Past `validUntil` |
| `revoked` | Superadmin revoked |

Gate behavior:

- missing / inactive / expired / revoked / quota exhausted → **402**
- feature or provider source not allowed → **403**
- seat limit reached → invite/accept rejected

### 5. Revoke or replace

On `/superadmin/licenses`, **Revoke** (confirm dialog). Requires a fresh 15-minute session.

If that row is the workspace’s current active entitlement:

- license → `revoked`
- workspace → `pending_license`
- queued/dialing/active calls for that license are canceled
- LiveKit rooms are queued for termination

To replace: revoke the current one, issue a new key, activate again as the tenant. You cannot activate a second live license beside an unexpired active one.

---

## Operator checklist (first customer)

1. `create-superadmin` with a real email; store the password privately.
2. Create the tenant (register or `create-tenant-admin`). Note `workspace_id`.
3. Sign in as superadmin → `/superadmin/licenses` → issue for that ID.
4. Copy the one-time key once; send it securely.
5. Tenant owner signs in → `/settings` → paste → Activate.
6. Confirm plan, seats, dates, provider mode, quotas.
7. Add BYOK keys only for kinds the signed policy allows.
8. Invite members up to the seat limit.
