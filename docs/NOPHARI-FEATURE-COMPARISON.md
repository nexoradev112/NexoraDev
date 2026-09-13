# Nophari feature comparison

This is a functional gap map, not a claim of source-code equivalence,
endorsement, or pixel-for-pixel cloning. It records the requested public/demo
capabilities and the status of this independent implementation.

| Capability | This product | Status / boundary |
| --- | --- | --- |
| Visual branching flowchart | React Flow studio with typed nodes, edges, conditions, save/load, versions, import/export, and templates | Implemented v1 |
| Generate agent from a use case | Server-side LLM generation produces an editable agent and graph | Implemented v1; requires a licensed configured LLM |
| Talk test from agent list/studio | Browser receives a short-lived LiveKit token and joins the Python voice worker | Implemented v1; requires trusted TLS, microphone permission, LiveKit, and STT/LLM/TTS credentials |
| Call test from agent list | A licensed operator can queue an outbound test through an operator-configured LiveKit SIP trunk, with consent, suppression, quota, and post-dispatch license checks | Implemented v1; each carrier trunk still requires operator provisioning and country-specific certification |
| Post-call webhook node | Durable ordered executor, encrypted integration credentials, signed/idempotent delivery, retry leases, SSRF/DNS-rebinding controls, bounded payloads/responses, and sanitized delivery results | Implemented v1 |
| Post-call LLM QA node | Durable rubric evaluation, provider-policy and quota enforcement, PII-redacted stored results, score/sentiment/disposition updates, and dependency ordering before webhooks | Implemented v1 |
| Twilio, Vonage, Vobiz, ConVox, Telnyx, Cloudonix, Asterisk | Provider names, encrypted credential storage, and phone inventory are modeled | Individual dialing/provisioning adapters and carrier certification require phase two integration tests |
| Live ON AIR occupancy | Tenant-scoped active/queued call sessions grouped by agent/provider | Implemented v1; depends on worker completion and stale-reservation cleanup |
| Credits, sentiment, pipeline, dispositions | Call/usage data is aggregated into dashboard and analytics responses | Implemented v1; accuracy depends on trusted completion data and configured classification |
| Daily reports | Timezone/day/agent filtering, duration buckets, completion/transfer rates, disposition totals, CSV | Implemented v1 |
| Audio prompt/transition library | Tenant-scoped upload/list/delete, authenticated content access, and graph-bound Audio-node playback | Implemented v1; automatic transcription/extraction is not claimed |
| Arabic, US/UK English, Hindi, Hinglish | Agent locale policy and provider language selection | Implemented configuration; production quality must be tested per provider, voice, accent, and code-switch case |

## Additional product capabilities

The independent product adds controls that are central to a licensed SaaS:

- separate superadmin license issuer and tenant workspace administration;
- one-time license keys with signed dates, seats, features, quotas, and provider
  policy;
- `byok`, `platform`, and explicit per-kind `hybrid` credential modes;
- AES-GCM tenant provider vault with no secret-return API and no BYOK fallback;
- workspace-scoped RBAC and email-bound expiring invites;
- agent versions and credential-free definition import/export;
- CMS-managed marketing content and media;
- Python voice rails that cannot be disabled by a Studio node or BYOK model;
- one-droplet Docker Compose deployment with PostgreSQL, local files, nginx,
  LiveKit, Redis, backup, restore, and optional LiveKit Cloud override.

## Important voice constraint

The one-droplet edition runs one shared worker connected to one LiveKit project.
Voice licenses must map `realtime` to `platform`; use hybrid mode for tenant BYOK
LLM/STT/TTS. Serving a different tenant-owned LiveKit project per workspace
requires a tenant-partitioned worker pool and is not implemented here.

## Phase-two order

1. Carrier adapters for outbound/inbound SIP, number provisioning, consent and
   suppression checks, and real human transfer confirmation.
2. Campaign scheduler/dispatcher and encrypted contact ingestion.
3. Knowledge extraction, embeddings, retrieval, deletion, and tenant isolation.
4. Optional Stripe subscriptions mapped to license issuance/renewal rather than
   replacing the signed license gate.
