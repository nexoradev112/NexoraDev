# Launch guide

This checklist turns the repository into a low-volume licensed SaaS on one
Ubuntu droplet. The exact commands and firewall details are maintained in
[DROPLET-DEPLOYMENT.md](DROPLET-DEPLOYMENT.md).

## 1. Decide the commercial boundary

For the first release, sell browser chat and browser Talk with licensed agents,
roles, seats, provider modes, quotas, analytics, reports, recordings, and CMS.
Do not sell campaigns, automatic knowledge extraction, post-call action
execution, billing automation, carrier dialing, or real SIP transfer until the
phase-two services and compliance tests exist.

Choose:

- the countries and supported use cases;
- whether tenant self-registration is enabled;
- which provider kinds the platform pays for;
- default seat, agent, token, and voice-second limits;
- retention, recording, consent, privacy, and human-escalation policies;
- support and incident-response contacts.

## 2. Prepare the host

1. Provision Ubuntu 24.04 LTS with at least 4 GB RAM for a low-volume start.
2. Enable DigitalOcean account MFA, SSH keys, automatic security updates, a
   cloud firewall, and restricted administrative access.
3. Point separate app and voice DNS names to the droplet.
4. Install Docker Engine and Compose from Docker's supported Ubuntu packages.
5. Place the reviewed release at `/opt/nexora`.
6. Allow only SSH, 80/TCP, 443/TCP, 7881/TCP, and the configured LiveKit UDP
   range. Restrict SSH to administrator IPs where possible.

## 3. Initialize and boot

```bash
cd /opt/nexora
sudo ./scripts/init-droplet.sh app.example.com voice.example.com /var/lib/nexora
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Add platform provider keys to `.env` only for kinds you will license as
`platform`. Do not add browser-prefixed secrets. Check logs for startup errors
without printing configuration values.

## 4. Install trusted TLS

Replace the generated smoke-test certificate with a trusted certificate using
the Certbot webroot procedure in `DROPLET-DEPLOYMENT.md`. Configure renewal and
test both the app HTTPS origin and voice WSS origin externally. Do not accept
customer signup, passwords, API keys, or microphone permissions on the
self-signed certificate.

## 5. Bootstrap the product

```bash
docker compose run --rm api \
  python -m app.cli create-superadmin --email owner@example.com
```

Then use two separate browser accounts:

1. register the tenant owner and workspace;
2. sign in as superadmin and issue a license to that exact workspace;
3. store the one-time license key in the intended secure delivery channel;
4. activate the key as tenant owner;
5. verify expiry, seats, features, quotas, and provider mode;
6. save the permitted provider credentials;
7. invite a normal user and accept only from the invited email address.

Tenant admins cannot issue or revoke licenses.

## 6. Choose a voice-compatible provider policy

The single shared worker uses the deployment's LiveKit transport. Voice
licenses must therefore use `platform`, or `hybrid` with
`realtime: platform`. To let a tenant own inference costs, map `llm`, `stt`, and
`tts` to `byok`; map every other kind explicitly as well. Do not issue pure
BYOK voice licenses on the one-worker edition.

Provider support in the direct worker is:

| Kind | Supported providers |
| --- | --- |
| LLM | OpenAI, Groq, Anthropic |
| STT | Deepgram, OpenAI, ElevenLabs |
| TTS | ElevenLabs, OpenAI, Deepgram |
| Realtime | deployment LiveKit project |

Arabic, US/UK English, Hindi, and Hinglish depend on both the agent locale and
the selected model/voice. Run recorded test scripts with native speakers; a
locale option does not guarantee provider pronunciation or code-switch quality.

## 7. Acceptance test

Do not launch until all of these pass on the deployed hostname:

- correct and incorrect login, logout, session expiry, and rate limiting;
- superadmin-only license issue/revoke and tenant-admin denial;
- license not-yet-valid, expired, revoked, missing-feature, exhausted-quota,
  seat-limit, and modified-database-claim failures;
- two-tenant read/write/delete/export isolation checks;
- BYOK success and missing-BYOK failure while platform keys are present;
- agent create, graph branch save, reload, version, import/export, and use-case
  generation;
- browser chat with usage metering;
- browser Talk over trusted TLS, interruption, long silence, timeout, call
  completion, occupancy, analytics, and daily report;
- Python rail cases for payment cards, PII, jailbreaks, blocked topics,
  medical/legal claims, invented customer data, uncertainty, and Arabic/Hindi
  variants;
- recording upload, authorized playback in an Audio node, deletion, traversal,
  symlink, wrong-content, and oversized-file rejection;
- backup, checksum verification, and restore on a disposable droplet.

Run source tests and dependency scans from `README.md`. Record tool versions,
commit/archive checksum, results, failures, and approved exceptions.

## 8. Operate the first customers

- Start with a small concurrency limit and watch CPU, RAM, disk, UDP loss,
  provider latency, and completion records.
- Set finite token, voice-second, and agent quotas even for pilot plans.
- Keep at least daily off-host encrypted backups and test restoration regularly.
- Review provider invoices against usage counters; counters are product controls,
  not a substitute for provider budget alarms.
- Patch the host and rebuild pinned dependencies on a scheduled cadence.
- Require human escalation for uncertain or regulated-domain conversations.

The droplet is a single failure domain. Establish an upgrade and scale-out plan
before concurrency, recovery-time, or data-residency commitments exceed what one
host can meet.
