# Cost and plan guide

Provider and hosting prices change. This document intentionally does not embed
price claims; verify current vendor pricing, taxes, regions, minimums, and
telephony surcharges before publishing your own plans.

## Cost components

| Component | Main cost driver | Who can pay |
| --- | --- | --- |
| Droplet | vCPU, RAM, SSD, backup snapshots, outbound bandwidth | Platform |
| LiveKit transport | Included on the droplet by default; bandwidth and host capacity still apply | Platform |
| STT | Audio seconds/minutes, language/model, streaming premium | Tenant BYOK or platform |
| LLM | Input/output/cache tokens and selected model | Tenant BYOK or platform |
| TTS | Characters, tokens, or generated audio duration and voice tier | Tenant BYOK or platform |
| Telephony | Numbers, inbound/outbound minutes, SIP, recording, country fees | Tenant BYOK or platform |
| Storage/backup | PostgreSQL size, recordings, uploaded media, retention, off-host copies | Platform |
| Operations | Monitoring, email delivery, support, patches, restore tests, compliance | Platform |

Self-hosted LiveKit avoids making a managed LiveKit plan mandatory; it does not
make realtime voice free. The droplet must carry signaling/media load, internet
egress, Redis, and the Python worker. Networks needing TURN/TLS or global media
routing may justify LiveKit Cloud or separate TURN infrastructure.

## Estimate one voice session

Use vendor rate cards in one currency and billing unit:

```text
voice session cost =
    STT audio duration
  + LLM input/output tokens
  + TTS generated speech
  + realtime bandwidth/hosting allocation
  + telephony minutes (if a phone call)
  + recording storage and backup
  + operating margin for retries, support, and tax
```

Measure p50 and p95 call duration, user/agent speaking ratio, turns per minute,
prompt size, output tokens per turn, retries, and provider failure rate. A demo
script usually underestimates production silence, interruptions, and support
overhead.

## BYOK, platform, and hybrid economics

- `byok`: the tenant is billed directly by each selected provider. You still pay
  for the droplet, transport, storage, support, and abuse response.
- `platform`: you pay providers. Use signed license quotas, provider budget
  alerts, and a margin that covers retries and bad debt.
- `hybrid`: the signed policy assigns each kind. A useful single-droplet voice
  policy is tenant BYOK for LLM/STT/TTS and platform for realtime. Telephony is
  assigned explicitly.

There is no silent cost transfer: if a BYOK credential is absent, the request
fails instead of consuming a platform key.

## License-plan controls

Use all available server-enforced limits:

- `seats` for active workspace membership and pending invites;
- `agents` for agent count;
- `tokens` for platform LLM consumption;
- `voice_seconds` for completed usage and pre-call reservations;
- `features` to remove unavailable surfaces such as voice, recordings,
  analytics, members, or telephony.

Keep limits finite for pilots. Alerts should trigger before the signed hard
quota so customers can renew without service interruption. HTTP 402 is the
expected hard-stop behavior for expired/revoked licenses and exhausted metered
quota.

## Lowest-cost launch pattern

1. Start with browser chat and a small number of browser voice testers.
2. Use one 4 GB+ droplet only after measuring idle and one-call memory.
3. Offer BYOK or hybrid inference to avoid carrying variable provider spend.
4. Do not enable phone numbers, campaigns, or recording retention until a paying
   use case requires them.
5. Cap call duration, model output, uploads, recording size, and retention.
6. Use smaller models for routing/QA only after testing quality in Arabic,
   English, Hindi, and Hinglish.
7. Store backups off-host but keep a defined retention window.

Do not reduce cost by weakening TLS, backups, tenant isolation, consent checks,
provider separation, or Python rails.

## When to leave one droplet

Plan a move when measured concurrency saturates CPU/RAM/network, local-disk
recovery time exceeds your promise, a tenant requires data residency or high
availability, or multiple LiveKit projects require tenant-partitioned workers.
Budget for managed PostgreSQL, object storage, worker orchestration, TURN/global
media, centralized secrets, shared rate limits, monitoring, and migration work
before making those commitments.
