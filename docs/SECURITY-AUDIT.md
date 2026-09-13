# Security review and release boundary

Last documentation review: 2026-09-09.

This document records controls visible in the one-droplet source and known
release risks. It is not a penetration-test report, compliance certification,
or guarantee that the product has no vulnerabilities. Record actual test,
dependency-scan, configuration, and end-to-end results for every release.

## Scope reviewed

- Next.js server proxy and browser authentication flow;
- FastAPI authentication, tenancy, license, provider, agent, file, chat, voice,
  and worker endpoints;
- PostgreSQL models and transaction boundaries;
- Python provider construction and safety rails;
- Docker Compose, nginx, TLS bootstrap, secrets, backup, and restore scripts.

Cloud provider consoles, DNS, DigitalOcean account controls, third-party API
accounts, carrier registration, customer workflows, and the running host are
outside this source review.

## Implemented controls observed in source

| Boundary | Control |
| --- | --- |
| Passwords and sessions | Argon2 password hashing, password policy, dummy-hash login path, hashed random session tokens, absolute/idle expiry, logout revocation, Secure HTTP-only `__Host-` cookies in production |
| Request boundary | Exact production origins, origin checks on state-changing requests, body-size caps, nginx/API rate limits, no-store API responses, security headers, and public denial of `/api/internal/*` |
| Tenant isolation | Membership-derived workspace access, workspace predicates on tenant data, scoped roles, signed feature checks, and non-authoritative workspace selectors |
| Licenses | One-time activation key, stored key hash/prefix, Ed25519 signed claims, active/date/revocation checks, seat enforcement, quota counters, and feature enforcement |
| Provider keys | AES-GCM at rest with record/workspace/kind/provider AAD; responses expose metadata only; BYOK has no implicit platform fallback |
| Worker channel | Private network, bearer authentication, HMAC timestamp/body binding, persisted nonce replay rejection, and a separate room-bound encrypted provider envelope |
| Voice safety | Platform-owned Python input/output/tool/persistence rails, hard duration cap, deny-by-default external-action allowlists, and fail-closed outbound/recording consent checks |
| Deployment | Non-root containers, dropped capabilities, `no-new-privileges`, read-only roots, private networks, unexposed data services, log rotation, and bind-mounted persistent data |
| Recovery | PostgreSQL/files backup scripts, checksums, pre-restore backup, and explicit destructive-restore confirmation |

## Security properties that require verification

Before a public release, verify rather than assume:

- tenant A cannot read, update, delete, export, play, or invoke any tenant B
  agent, provider, file, recording, call, invite, or report;
- an expired or revoked license receives HTTP 402 on every licensed tenant API;
- a missing feature receives HTTP 403 and the UI does not substitute a weaker
  path;
- concurrent invite acceptance cannot exceed seats;
- concurrent chat and voice starts cannot exceed token/voice quotas;
- BYOK mode with a missing exact provider key fails closed even when platform
  keys are present in the API container;
- provider secrets never appear in API JSON, HTML, room metadata, participant
  attributes, logs, exception messages, source maps, or release archives;
- signed worker requests reject modified bodies, old timestamps, and reused
  nonces;
- model output is buffered and accepted by Python rails before the first TTS
  frame, including fragmented payment-card and multilingual cases;
- outbound/recorded calls cannot start without the expected consent evidence;
- file upload, download, symlink, path traversal, content-type, size, and
  decompression cases stay inside the workspace namespace;
- trusted TLS, HSTS, cookie scope, LiveKit WSS, ICE/TCP, and ICE/UDP work from an
  external network.

Suggested release commands are in `README.md`. Also test the complete tenant
lifecycle through the browser with a separate second tenant and intentionally
invalid licenses/keys.

## Known limitations and residual risk

1. **Single-host secret concentration.** PostgreSQL, encrypted credentials, the
   AES key, worker secrets, and the license signing key reside on one machine.
   AES-GCM protects a database-only theft, not root compromise. Use DigitalOcean
   MFA, restricted SSH, timely patches, encrypted off-host backups, and a secret
   manager/HSM design for higher assurance.
2. **License issuer online and rollbackable.** The private Ed25519 key is available to FastAPI so
   the superadmin UI can issue licenses. A compromised API host can mint signed
   licenses. Immutable claims and lifecycle state are separately signed, so a
   database-only field edit is rejected; a privileged operator could still
   restore an older complete database image with its matching signature.
   Offline signing and an off-box append-only lifecycle ledger are not implemented.
3. **Rate limiting is installation-local.** nginx provides edge limits and the
   API has a bounded in-process authentication limiter. The latter resets on
   restart and is not suitable for a multi-API deployment without Redis-backed
   coordination.
4. **CSP still permits inline script/style required by the current Next build.**
   Preserve output encoding and remove those allowances when nonce/hash support
   is implemented.
5. **Safety detection is finite.** Regex and phrase checks do not detect every
   language, obfuscation, medical/legal claim, or invented fact. Keep human
   escalation, narrow agent scope, provider moderation where appropriate, and
   adversarial language tests.
6. **Uploads are not an antivirus pipeline.** Do not allow uploaded files to be
   executed. Add malware scanning and document sandboxing before broad
   knowledge-file ingestion.
7. **Some integrations remain phase two.** Campaign dispatch, Stripe, document
   extraction, carrier-specific provisioning beyond operator-configured LiveKit
   SIP trunks, and real human SIP transfer need their own threat models and
   deployment tests before activation.
8. **No independent assurance is included.** Commission a penetration test and
   review privacy, recording, telemarketing, data-residency, retention, and AI
   disclosure duties for every launch country.
9. **Account recovery is operator-assisted in v1.** MFA, verified-email signup,
   and self-service password reset are not implemented. Keep public tenant
   registration disabled, provision owners through the CLI, and add an audited
   email-delivery and recovery design before opening self-service signup.

## Secret-handling rules

- Never use `NEXT_PUBLIC_` or `VITE_` for a secret.
- Never commit `.env`, TLS private keys, backups, database files, recordings, or
  provider exports.
- Do not paste `docker compose config` output, provider errors, license keys, or
  backup secret archives into tickets or chat.
- Give the web and voice containers only the secrets they require. Platform AI
  provider keys remain in FastAPI; resolved inference keys reach the voice
  worker only in the short-lived encrypted envelope.
- Rotate any credential disclosed outside the designated secret store. Rotation
  must be followed by a chat/voice smoke test and, where relevant, tenant BYOK
  re-entry.

## Incident response minimum

If host or credential compromise is suspected: stop public traffic, preserve
logs and a forensic snapshot, revoke provider and telephony keys, invalidate
sessions, rotate worker/vault/LiveKit secrets, revoke affected licenses, notify
tenants according to law and contract, rebuild from a known release, restore
validated data, and document the root cause. Rotating the vault key requires an
explicit re-encryption migration; simply changing it makes existing BYOK data
unreadable.
