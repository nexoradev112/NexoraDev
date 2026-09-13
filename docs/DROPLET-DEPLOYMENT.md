# Single-droplet production deployment

This deployment runs the product on one Ubuntu host. Cloudflare Workers, D1,
R2, and ChatGPT identity headers are not part of the production path.

## What runs

| Service | Purpose | Public ports |
| --- | --- | --- |
| nginx | TLS, security headers, app/API routing, LiveKit WSS | 80/TCP, 443/TCP |
| Next.js | Marketing site, dashboard, studio | None |
| FastAPI | Auth, tenancy, licenses, providers, agents, files | None |
| PostgreSQL | Tenant data and usage ledger | None |
| LiveKit | Self-hosted realtime transport | 7881/TCP, 50000-50100/UDP |
| Redis | LiveKit coordination | None |
| Python voice agent | Voice pipeline and mandatory safety rails | None |
| Dispatcher | Optional phase-two campaigns worker | None |

PostgreSQL and application files are bind-mounted below `/var/lib/nexora` by
default. Files remain partitioned beneath `/var/lib/nexora/files` with tenant
paths such as `knowledge/w<workspace-id>/` and `recordings/w<workspace-id>/`.
Only FastAPI receives that files mount; nginx never serves it directly.

The default realtime transport is self-hosted LiveKit plus Redis. LiveKit's
official firewall guide identifies 7881/TCP as ICE/TCP and a configurable UDP
range for ICE/UDP. The included range has 101 ports; expand it only after load
testing concurrent participants. See [LiveKit ports and firewall](https://docs.livekit.io/transport/self-hosting/ports-firewall/)
and [LiveKit VM deployment](https://docs.livekit.io/transport/self-hosting/vm/).

## 1. Prepare Ubuntu and DNS

Use Ubuntu 24.04 LTS with at least 4 GB RAM for a low-volume launch. More
concurrent calls, direct local inference, SIP, or media transcoding need more
CPU/RAM and should be load-tested before sale.

1. Install Docker Engine and the Docker Compose plugin from Docker's official
   Ubuntu repository.
2. Put the repository at `/opt/nexora`.
3. Create two DNS A/AAAA records pointing to the droplet:
   `app.example.com` and `voice.example.com`.
4. In both UFW and the DigitalOcean Cloud Firewall, allow only:

   ```bash
   sudo ufw allow OpenSSH
   sudo ufw allow 80/tcp
   sudo ufw allow 443/tcp
   sudo ufw allow 7881/tcp
   sudo ufw allow 50000:50100/udp
   sudo ufw enable
   ```

Do not expose 3000, 5432, 6379, 7880, 8000, or the voice-agent health port.

## 2. Generate local secrets and storage

From the repository root:

```bash
sudo ./scripts/init-droplet.sh app.example.com voice.example.com /var/lib/nexora
```

The initializer refuses to overwrite an existing `.env` or rotate keys. It:

- generates independent database, Redis, LiveKit, worker HMAC, worker-envelope,
  and AES-GCM credential-vault secrets;
- generates raw Ed25519 license signing material in the format FastAPI validates;
- creates PostgreSQL and tenant-file directories with restricted ownership.

LiveKit and Redis are ordinary default services, so `docker compose up` always
starts a coherent browser-voice transport unless the cloud override is used.

Review `.env` without pasting it into tickets or chat. Add only provider keys
you intend to sell in `platform` mode. BYOK and hybrid resolution are enforced
by FastAPI; the web and voice containers are not given the platform-key
environment variables.

Never name a secret with `NEXT_PUBLIC_` or `VITE_`. Those prefixes are reserved
for browser-visible values.

nginx accepts at most 26 MiB per request, matching FastAPI's 25 MiB file limit
plus its one MiB request-envelope allowance. Keep `MAX_UPLOAD_SIZE=26m` and
`MAX_UPLOAD_BYTES=26214400` aligned if you deliberately change this ceiling.

## 3. Start and bootstrap

Validate interpolation without printing the rendered configuration, then start:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

The API entrypoint acquires a PostgreSQL advisory lock before applying schema
migrations. This prevents two API starts from racing a migration. A failed
migration fails the API container instead of serving a mixed schema.

Create the first platform operator interactively:

```bash
docker compose run --rm api \
  python -m app.cli create-superadmin --email owner@example.com
```

The command prompts for an email and password without putting the password in
shell history. Then use the product flow:

1. Register the tenant owner and pending workspace.
2. Sign in as superadmin and issue a license to that workspace.
3. Copy the license once and activate it from the tenant License screen.
4. Add members only up to the signed seat limit.
5. Select BYOK, platform, or an explicitly mapped hybrid mode.
6. Create an agent, save its graph, and use the browser voice test.

The license gate runs in FastAPI for every tenant endpoint. The workspace header
is only a selector; the authenticated membership remains the authorization
source.

License claims and lifecycle state are Ed25519 signed. This detects direct
database edits, but a single-host installation cannot prevent a privileged
operator from restoring an older full database snapshot with its matching
signature. Use an off-box append-only verifier if rollback resistance is a
deployment requirement.

## 4. Replace the smoke-test TLS certificate

On first start, nginx generates a seven-day self-signed certificate covering
both hostnames. It is only for a local smoke test. Browser microphone APIs and
real users require a trusted certificate.

One Let's Encrypt approach is Certbot's webroot mode. The HTTP challenge path is
already mounted at `/opt/nexora/deploy/acme`:

```bash
sudo certbot certonly --webroot \
  -w /opt/nexora/deploy/acme \
  -d app.example.com \
  -d voice.example.com

sudo /opt/nexora/scripts/install-tls-cert.sh \
  /etc/letsencrypt/live/app.example.com/fullchain.pem \
  /etc/letsencrypt/live/app.example.com/privkey.pem
```

Use the same install command as a Certbot deploy hook so renewal atomically
replaces both files and reloads nginx. Do not enable public signups or enter API
keys while the self-signed certificate is in use.

## 5. Realtime behavior

nginx terminates `wss://voice.example.com` and proxies signaling to LiveKit on
the private Compose network. WebRTC media goes directly to the droplet over
7881/TCP or the UDP range. The FastAPI token endpoint returns only the public
WSS URL and a short-lived room token; it never returns the LiveKit API secret.

Self-hosted LiveKit does not include LiveKit Inference. The Python agent uses
direct provider plugins selected by the tenant's enforced provider mode. The
control plane may disclose a decrypted provider credential only to the
authenticated worker channel for the exact tenant/job; it must never send it to
the browser, room metadata, participant attributes, or logs. See the
[LiveKit self-hosting comparison](https://docs.livekit.io/transport/self-hosting/).

For networks that require TURN/TLS on 443, use LiveKit Cloud or add a separate
TURN design/IP after testing; nginx already owns TCP 443 on this single IP.

### Optional LiveKit Cloud switch

Set `LIVEKIT_CLOUD_URL`, `LIVEKIT_CLOUD_API_KEY`, and
`LIVEKIT_CLOUD_API_SECRET` in `.env`, then run:

```bash
docker compose \
  -f docker-compose.yml \
  -f deploy/compose.livekit-cloud.yml \
  up -d --build --remove-orphans
```

The override omits local LiveKit/Redis, points FastAPI and the agent to the cloud
project, and rebuilds nginx without a local LiveKit upstream. Keep the mode
explicit; do not automatically fall back between cloud and self-hosted
credentials.

## 6. Operations

### Health and logs

```bash
docker compose ps
docker compose logs --since=15m api web voice-agent livekit nginx
```

Docker logs rotate at 10 MB with five files per service. Secret values must not
appear in normal logs. nginx uses a query-free structured access format: it logs
the normalized path (`$uri`) but never `$request`, `$request_uri`, `$args`, the
referrer, or query strings that can carry invite/room tokens. Do not use
`docker compose config` without `--quiet` in a shared terminal because rendered
environment values may be sensitive.

### Migrations

Migrations run automatically on API start. To run one explicitly while the API
is stopped:

```bash
docker compose stop api
docker compose run --rm api python /usr/local/lib/nexora/migrate-with-lock.py
docker compose up -d api
```

### Backups

```bash
sudo ./scripts/backup-droplet.sh /var/backups/nexora
```

Each timestamped backup contains a custom PostgreSQL dump, tenant files, checksums,
and a separate sensitive archive with `.env` including the license signer. Encrypt and
copy the whole directory off the droplet. Without the AES-GCM key, BYOK secrets
cannot be recovered; without the Ed25519 private key, the installation cannot
issue licenses compatible with its existing public key.

Test restoration on a disposable droplet. On the target host, the destructive
restore command makes an automatic pre-restore backup before replacing the
database schema and tenant files:

```bash
sudo ./scripts/restore-droplet.sh \
  /var/backups/nexora/20260101T000000Z \
  --confirm-destructive-restore
```

For a full disaster recovery, restore `secrets.tar.gz` into a fresh repository
before starting Compose, verify mode `0600` on the restored `.env`, then restore
the DB/files. Never email the secrets archive unencrypted.

### Upgrades

1. Create and copy an off-host backup.
2. Pull the reviewed release or unpack it into a new directory.
3. Preserve `.env`, `deploy/tls`, and `/var/lib/nexora`.
4. Run `docker compose config --quiet`.
5. Run `docker compose up -d --build --remove-orphans`.
6. Check health and complete a licensed chat/voice smoke test.

For a controlled production release, resolve and review the upstream Node,
Python, PostgreSQL, Redis, nginx, uv, and LiveKit image digests, then pin those
approved digests in the release manifest. Do not invent or reuse an unverified
digest; refresh pins deliberately when applying security updates.

Do not run two releases against the same files directory. PostgreSQL supports
multiple writers; local tenant files do not support concurrent deployments that
mutate the same object path.

## 7. Boot with systemd

Docker already restarts individual containers. To make the whole Compose stack
an explicit systemd unit:

```bash
sudo cp deploy/systemd/nexora-stack.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nexora-stack.service
```

The other units in `deploy/systemd/` are hardened native-process examples for
operators who intentionally run FastAPI, Next.js, and the voice worker outside
Docker. Do not enable those at the same time as the Compose services. The native
path also requires a host PostgreSQL/Redis/LiveKit setup and the sample
`deploy/nginx-host.conf.example`; Docker Compose is the supported default.

## Production checklist

- Trusted TLS works for both app and voice hostnames.
- Only 80, 443, 7881/TCP, and the configured UDP media range are public.
- `.env` (including raw license keys), TLS private key, backups, DB files, and tenant
  files are absent from images and Git.
- A non-superuser SCRAM-authenticated PostgreSQL role is used by FastAPI.
- Superadmin, license, membership, provider-mode, quota, and tenant-isolation
  negative tests pass.
- BYOK mode fails closed when its exact key is absent.
- Outbound calls fail closed without consent.
- Python input/output rails and call-length enforcement are active in the actual
  voice path, not only represented as studio nodes.
- Restore has been tested and secrets are stored encrypted off-host.
