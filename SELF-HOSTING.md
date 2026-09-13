# Self-hosting on one Ubuntu droplet

This is the short path for operators. Use
[docs/DROPLET-DEPLOYMENT.md](docs/DROPLET-DEPLOYMENT.md) for exact firewall,
TLS, backup, restore, upgrade, and optional LiveKit Cloud instructions.

## Supported production layout

- nginx is the only HTTP entry point on ports 80 and 443.
- Next.js serves marketing, dashboard, studio, and administration screens.
- FastAPI owns authentication, authorization, licenses, providers, agents,
  files, chat, LiveKit tokens, and worker callbacks.
- PostgreSQL is the system of record. SQLite is for isolated tests only.
- tenant media is on a bind-mounted local disk below `/var/lib/nexora/files`.
- LiveKit and Redis provide realtime transport; the Python voice worker provides
  the agent pipeline and mandatory safety rails.

Do not expose ports 3000, 5432, 6379, 7880, 8000, or the worker health port.
Open 7881/TCP and the configured LiveKit UDP media range in addition to 80/443.

## Prerequisites

- Ubuntu 24.04 LTS, Docker Engine, and the Docker Compose plugin;
- at least 4 GB RAM for a low-volume launch, followed by load testing for your
  actual concurrency and languages;
- two DNS names, for example `app.example.com` and `voice.example.com`;
- outbound HTTPS access to the selected STT, LLM, TTS, and optional telephony
  providers;
- encrypted off-host backup storage.

## Initialize and start

```bash
sudo ./scripts/init-droplet.sh app.example.com voice.example.com /var/lib/nexora
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Do not run `docker compose config` without `--quiet` in a shared terminal; the
rendered output may include secrets. The API start command holds a PostgreSQL
advisory lock and runs the v1 schema bootstrap. That bootstrap creates missing
tables; schema-changing upgrades should ship reviewed Alembic migrations before
you deploy them to an existing database.

Create the first platform operator without placing a password in shell history:

```bash
docker compose run --rm api \
  python -m app.cli create-superadmin --email owner@example.com
```

The command prompts twice for a password.

## Install trusted TLS

The first start creates a short-lived self-signed certificate for smoke tests.
Use the Certbot webroot and certificate-install commands in
`docs/DROPLET-DEPLOYMENT.md` before public use. Browser microphone access and
secure `__Host-` sessions require the real HTTPS origin.

## Configure license and provider policy

The superadmin creates a signed license with dates, seats, features, quotas,
and one provider mode:

| Mode | Credential source |
| --- | --- |
| `byok` | Every provider kind must come from the tenant vault. There is no platform fallback. |
| `platform` | Every provider kind uses a configured server environment key and consumes licensed quotas. |
| `hybrid` | The signed license explicitly maps `llm`, `stt`, `tts`, `realtime`, and `telephony` to `byok` or `platform`. No kind is inferred. |

For browser voice on the single shared worker, use `platform`, or use `hybrid`
with `realtime: platform`. A typical tenant-owned inference policy is:

```json
{
  "llm": "byok",
  "stt": "byok",
  "tts": "byok",
  "realtime": "platform",
  "telephony": "byok"
}
```

The API never silently substitutes a platform key when a required BYOK record
is absent. Provider-list responses contain metadata only, not secret material.

## Verify the tenant flow

1. Register a tenant owner and workspace.
2. Issue the workspace license in `/superadmin/licenses`.
3. Activate it in `/settings`; confirm dates and seat count.
4. Save the licensed provider keys and invite one email-bound member.
5. Accept the invite using the intended email; verify another tenant cannot
   select that workspace using `x-workspace-id`.
6. Create and save an agent in `/studio`.
7. Test `/api/chat`.
8. After trusted TLS is active, use Talk and verify the Python worker connects,
   enforces its call limit, and submits the completion record.

Phone-provider records and inventory are available in v1, but production
outbound carrier dialing and real SIP transfer require carrier-specific phase-two
adapters, regulatory configuration, and end-to-end tests.

## Data and recovery

The default persistent paths are `/var/lib/nexora/postgres` and
`/var/lib/nexora/files`. Run:

```bash
sudo ./scripts/backup-droplet.sh /var/backups/nexora
```

Copy the resulting database dump, files, and encrypted secrets archive off the
droplet. Test `scripts/restore-droplet.sh` on a disposable host. Losing
`CREDENTIAL_MASTER_KEY` makes stored BYOK credentials unrecoverable; losing the
license signing key prevents compatible license issuance.
