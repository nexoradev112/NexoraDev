# Nexora licensed AI agent platform

Nexora is a self-hosted, multi-tenant voice and chat agent product. The name is
a CMS-editable placeholder. The supported production path runs on one Ubuntu
droplet with Next.js, FastAPI, PostgreSQL, local tenant files, self-hosted
LiveKit, Redis, a Python voice worker, and nginx. Cloudflare Workers, D1, R2,
and ChatGPT identity headers are not production dependencies.

## What v1 implements

| Area | Implemented behavior |
| --- | --- |
| Authentication | Email/password accounts, Argon2 password hashes, server-side hashed sessions, secure cookies, logout, and a separate superadmin flag |
| Tenancy | Workspace-scoped `owner`, `admin`, `operator`, `member`, and `viewer` roles; membership is checked server-side for every tenant query |
| Licensing | Superadmin issue/list/revoke, one-time license-key display, tenant activation, Ed25519-signed entitlements, dates, seats, features, provider mode, and quotas |
| Providers | AES-GCM encrypted tenant BYOK records and explicit `byok`, `platform`, or signed per-kind `hybrid` selection; missing BYOK keys fail closed |
| Agents | React Flow studio, branching graph save/load, versions, import/export, templates, use-case generation, browser chat, and browser Talk |
| Voice | Self-hosted LiveKit transport, direct STT/LLM/TTS plugins, Python session lifecycle, audio-node playback, completion records, and mandatory Python rails |
| Operations | Dashboard, live ON AIR occupancy, call history, analytics, daily/CSV reports, recordings library, CMS, provider management, member invites, and telephony inventory |
| Languages | Agent locale policies for Arabic, US English, UK English, Hindi, Hinglish, and auto-detection where the chosen provider supports it |

The visual `Guardrail` workflow node means human approval. It is not a switch
for platform safety. Python input, tool, output, persistence, call-duration,
and outbound-consent checks run independently in the platform-owned worker.

## Deliberate v1 boundaries

The following are not represented as production-complete:

- campaign scheduling/dispatch and bulk contact ingestion;
- Stripe subscriptions and metered invoicing;
- automatic document extraction, chunking, embeddings, and RAG retrieval;
- carrier-specific outbound SIP adapters and real SIP human transfer;
- high availability, multi-region operation, or zero-downtime file-store
  failover.
- MFA, email verification, and password-reset/recovery flows.

Real browser voice also requires provider credentials, working DNS and firewall
rules, a microphone-capable browser, and trusted TLS. A successful container
health check is not an end-to-end voice test.

## Start on one droplet

The authoritative production runbook is
[docs/DROPLET-DEPLOYMENT.md](docs/DROPLET-DEPLOYMENT.md).

```bash
sudo ./scripts/init-droplet.sh app.example.com voice.example.com /var/lib/nexora
docker compose config --quiet
docker compose up -d --build
docker compose run --rm api \
  python -m app.cli create-superadmin --email owner@example.com
```

The initializer creates `.env`, independent cryptographic keys, and the
PostgreSQL/files directories. It refuses to overwrite an existing `.env`.
Replace the smoke-test certificate with a trusted certificate before public
signup, API-key entry, or microphone testing.

Then:

1. Register the tenant owner and workspace.
2. Sign in as the superadmin and issue a license to that workspace.
3. Copy the license key from the one response in which it is shown.
4. Activate it as the tenant owner/admin.
5. Add provider credentials allowed by the signed provider policy.
6. Create an agent, save its graph, test chat, then test browser voice.

## Provider-mode rule for the one-worker edition

One shared LiveKit worker connects to one LiveKit transport using its process
environment. Therefore voice-enabled licenses on this one-droplet deployment
must map `realtime` to `platform`. Tenant-owned LLM, STT, and TTS keys remain
supported by issuing a `hybrid` license whose signed policy maps those kinds to
`byok` and `realtime` to `platform`. A pure BYOK license can be used for
non-voice features, but its tenant-owned LiveKit project cannot be served by the
single shared worker. A per-tenant worker pool is a separate scale-out design.

## Documentation

- [Self-hosting overview](SELF-HOSTING.md)
- [Local PostgreSQL](docs/LOCAL-DATABASE.md)
- [Single-droplet operations](docs/DROPLET-DEPLOYMENT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Security review and open risks](docs/SECURITY-AUDIT.md)
- [Launch checklist](docs/LAUNCH-GUIDE.md)
- [Cost model](docs/COST-GUIDE.md)
- [Nophari feature comparison](docs/NOPHARI-FEATURE-COMPARISON.md)
- [Python voice worker](services/livekit-agent/README.md)

## Release checks

Run checks from the repository root before packaging or upgrading:

```bash
npm ci
npm run lint
npm test
npm audit

cd backend
uv sync --extra dev --frozen
uv run ruff check .
uv run pytest

cd ../services/livekit-agent
uv sync --frozen
uv run python -m unittest discover -s tests -v
```

Record the actual results for the release. These commands do not replace an
independent penetration test, provider integration test, or jurisdictional
review for calling and recording.
