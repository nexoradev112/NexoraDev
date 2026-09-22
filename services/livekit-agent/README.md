# Python LiveKit voice worker

The worker is the persistent realtime execution process. It connects to the
deployment's LiveKit project, receives dispatched rooms, loads the exact
workspace/agent configuration from FastAPI, constructs direct STT/LLM/TTS
plugins, applies mandatory Python rails, and reports completion.

It does not depend on LiveKit Inference and does not read process-level
LLM/STT/TTS provider keys. FastAPI resolves the signed tenant provider policy and
sends each credential only in a short-lived encrypted worker envelope.

## One-droplet operation

The default `docker-compose.yml` starts self-hosted LiveKit, Redis, this worker,
FastAPI, Next.js, PostgreSQL, and nginx:

```bash
docker compose up -d --build
docker compose logs --since=15m voice-agent livekit api
```

Use `docs/DROPLET-DEPLOYMENT.md` for DNS, firewall, TLS, health, backup, and the
optional LiveKit Cloud override.

The worker needs these server-only values:

- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`;
- `APP_URL` pointing to the private FastAPI origin;
- `CALL_WORKER_TOKEN` for authenticated internal requests;
- `WORKER_CONFIG_KEY`, a distinct 32-byte base64url AES-GCM key;
- `PRIVATE_APP_ORIGINS=http://api:8000` and
  `ALLOW_PRIVATE_APP_URL=true` only for the exact private Compose origin;
- `PLATFORM_MAX_CALL_SECONDS` as the non-tenant-overridable ceiling.

Never use `NEXT_PUBLIC_` or `VITE_` names for these values.

## Shared LiveKit constraint

One worker process registers against one LiveKit project. Consequently,
voice-enabled licenses in the one-worker edition must map `realtime` to
`platform`, meaning the LiveKit credentials in the deployment environment.

Tenant BYOK inference is still supported through a signed hybrid policy, for
example LLM/STT/TTS as `byok` and realtime as `platform`. A pure BYOK license
whose realtime source is a separate tenant LiveKit project cannot dispatch to
this shared worker. Supporting that topology requires a worker pool keyed by
tenant/project.

## Control-plane contract

The worker keeps these endpoints stable:

- `POST /api/internal/agents/config`
- `POST /api/internal/recordings/content`
- `POST /api/internal/calls/complete`

Every request carries the worker bearer token and an HMAC over version,
timestamp, nonce, method, path, and SHA-256 body hash. FastAPI rejects stale
timestamps, modified requests, and nonce replay. Internal paths are not exposed
through nginx or the Next API proxy.

The config response includes the agent and `runtimeProvidersEnvelope`:

```json
{
  "v": 1,
  "alg": "A256GCM",
  "nonce": "base64url nonce",
  "ciphertext": "base64url ciphertext and tag"
}
```

The encrypted payload is bound to the exact room using AAD
`nexora-runtime-providers:v1:<roomName>`, expires within 120 seconds, and holds
an explicit mode, hybrid policy where applicable, and resolved `llm`, `stt`, and
`tts` objects. Each provider object names its `credentialSource`; the worker
rejects a source that does not match the signed mode. Plain provider
configuration is rejected by default.

Supported direct plugins are:

| Kind | Providers |
| --- | --- |
| LLM | OpenAI, Groq through the OpenAI-compatible plugin, Anthropic |
| STT | OpenAI, Deepgram, ElevenLabs |
| TTS | OpenAI, ElevenLabs, Deepgram |
| VAD | local Silero |

Provider availability and language quality must be verified with the exact
models and accounts used in production.

## Mandatory rails

`rails.py` is platform code. A workflow prompt, tenant setting, or BYOK model
cannot disable it. It is applied:

- before caller text reaches the model;
- at tool arguments and returned text;
- to the complete model response before the first TTS frame;
- before transcript persistence;
- at call setup and through a hard duration timer.

The deterministic layer bounds text, performs Luhn-aware card blocking, redacts
common phone/email/government-ID/IBAN values, detects supported jailbreak and
blocked-topic phrases, and enforces deny-by-default tool/webhook allowlists.
Outbound `call-*` rooms require server-verified calling consent; recorded
outbound sessions also require recording consent.

The output layer replaces supported medical/legal conclusions, invented
customer facts, or uncertainty with a safe human-handoff response. A Studio
`Guardrail` node means human approval and never changes the platform rails.

These controls reduce risk but are not comprehensive content moderation. Add
native-speaker red-team cases for Arabic, English, Hindi, and Hinglish and keep
a real human escalation path.

## Local development and tests

From `services/livekit-agent`:

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -v
```

For interactive provider testing, use a non-production LiveKit project and
test credentials, then run the LiveKit Agents console/development command
supported by the pinned worker version. Do not paste real keys into shell
history or commit a local `.env`.

An end-to-end browser voice test requires trusted TLS, working WSS and ICE
ports, microphone permission, a voice-compatible signed license, and valid
STT/LLM/TTS credentials. Unit tests and container health checks do not prove
that path.

## Human handoff simulator

On a human handoff the worker speaks "Please wait a moment while I transfer you."
and then leaves the room. The same leave happens when the agent itself says it
is connecting or transferring the caller, for example "Let me connect you with
a specialist." That sentence is played as spoken. Browser test rooms stay open.
Billable `call-*` rooms still close, because SIP transfer is not wired yet.

From `services/livekit-agent`, start this before or during the voice test:

```bash
python human_simulator.py
```

It uses the same `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET` as
the worker. After the voice agent disconnects, the test drawer shows
"Staff joined." Lines typed in that terminal are spoken into the room with the
Windows speech API. `/quit` leaves and waits for the next test-room handoff.

## v1 boundaries

- Post-call Webhook and QA nodes are stored by the Studio but are not executed
  by this worker in v1.
- A Handoff node records/requests escalation. Test rooms can be joined by
  `human_simulator.py`. Confirmed SIP transfer is phase two.
- Carrier-specific outbound dialing and campaigns are phase two.
- This worker is not a high-availability or per-tenant LiveKit worker pool.
