"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

type LicenseRow = { id: number; workspaceId: number; workspaceName?: string; plan: string; seats: number; status: string; providerMode: string; validFrom: string; validUntil: string; createdAt?: string };
type AudioReviewRow = { id: number; workspaceId: number; filename: string; contentType: string; size: number; locale: string; durationMs: number; safetyStatus: string; createdAt: string };

export default function LicenseConsole({ userName }: { userName: string }) {
  const now = useMemo(() => new Date(), []);
  const [rows, setRows] = useState<LicenseRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [issuedKey, setIssuedKey] = useState("");
  const [copied, setCopied] = useState(false);
  const [audioRows, setAudioRows] = useState<AudioReviewRow[]>([]);
  const [reviewNotes, setReviewNotes] = useState<Record<number, string>>({});
  const [reviewingId, setReviewingId] = useState<number | null>(null);
  const [form, setForm] = useState({ workspaceId: "", plan: "growth", seats: "5", validFrom: isoDay(now), validUntil: isoDay(new Date(now.getTime() + 365 * 86400_000)), providerMode: "hybrid", voiceSeconds: "36000", tokens: "2000000", agents: "10", hybridLlm: "byok", hybridStt: "byok", hybridTts: "byok", hybridRealtime: "platform", hybridTelephony: "byok" });

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [licenseData, audioData] = await Promise.all([
        json<{ licenses?: LicenseRow[] }>(await fetch("/api/superadmin/licenses", { cache: "no-store" })),
        json<{ recordings?: AudioReviewRow[] }>(await fetch("/api/superadmin/audio-review", { cache: "no-store" })),
      ]);
      setRows(licenseData.licenses || []);
      setAudioRows(audioData.recordings || []);
    } catch (reason) { setError(message(reason)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  async function issue() {
    setError(""); setIssuedKey(""); setCopied(false);
    try {
      const response = await fetch("/api/superadmin/licenses", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({
        workspaceId: Number(form.workspaceId), plan: form.plan, seats: Number(form.seats), validFrom: dayBoundary(form.validFrom, false), validUntil: dayBoundary(form.validUntil, true), providerMode: form.providerMode,
        hybridPolicy: form.providerMode === "hybrid" ? { llm: form.hybridLlm, stt: form.hybridStt, tts: form.hybridTts, realtime: form.hybridRealtime, telephony: form.hybridTelephony } : undefined,
        quotas: { voice_seconds: Number(form.voiceSeconds), tokens: Number(form.tokens), agents: Number(form.agents) },
        features: ["agents", "chat", ...(form.providerMode === "byok" ? [] : ["voice"]), "providers", "members", "analytics", "recordings", "telephony", "post_call"],
      }) });
      const data = await json<{ licenseKey?: string }>(response);
      const oneTimeKey = data.licenseKey || "";
      if (!oneTimeKey) throw new Error("The API did not return the one-time license key");
      setIssuedKey(oneTimeKey);
      await load();
    } catch (reason) { setError(message(reason)); }
  }

  async function revoke(id: number) {
    if (!window.confirm("Revoke this license now? Tenant requests will fail with payment-required status.")) return;
    setError("");
    try {
      await json(await fetch(`/api/superadmin/licenses/${id}/revoke`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ reason: "revoked_by_superadmin" }) }));
      await load();
    } catch (reason) { setError(message(reason)); }
  }

  async function copyKey() {
    await navigator.clipboard.writeText(issuedKey);
    setCopied(true);
  }

  async function reviewAudio(id: number, decision: "approved" | "rejected") {
    setError("");
    setReviewingId(id);
    try {
      const data = await json<{ recording?: AudioReviewRow }>(await fetch(`/api/superadmin/audio-review/${id}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ decision, note: reviewNotes[id] || "" }),
      }));
      if (data.recording) setAudioRows(current => current.map(row => row.id === id ? { ...row, ...data.recording } : row));
      setReviewNotes(current => { const next = { ...current }; delete next[id]; return next; });
    } catch (reason) { setError(message(reason)); }
    finally { setReviewingId(null); }
  }

  const pendingAudio = useMemo(() => audioRows.filter(row => row.safetyStatus === "pending_review"), [audioRows]);

  return <main className="superadmin-shell">
    <aside className="settings-side"><Link className="brand" href="/"><span className="brand-mark">N</span><span>NEXORA</span></Link><p>PLATFORM OPERATOR</p><nav><button className="on" type="button" onClick={() => { window.location.hash = "licenses"; }}>◆<span>Licenses</span></button><button type="button" onClick={() => { window.location.hash = "audio-review"; }}>♫<span>Audio review</span></button></nav><div className="settings-account"><b>{userName}</b><small>Superadmin</small><Link href="/dashboard">Tenant app</Link><Link href="/logout">Sign out</Link></div></aside>
    <section className="settings-main"><header><div><small>SUPERADMIN · LICENSE AUTHORITY</small><h1>Licenses</h1></div></header><div className="settings-content">
      {error ? <p className="form-error" role="alert">{error}</p> : null}
      {issuedKey ? <section className="one-time-license" aria-live="polite"><small>SHOWN ONCE</small><h2>Copy the signed license now</h2><code>{issuedKey}</code><button className="button" onClick={() => void copyKey()}>{copied ? "Copied" : "Copy license key"}</button><p>Only its SHA-256 hash is stored. Closing or refreshing this page removes this copy.</p></section> : null}
      <div className="settings-columns" id="licenses">
        <section className="settings-panel settings-form"><small>ISSUE LICENSE</small><h2>Tenant entitlement</h2><label>Workspace ID<input type="number" min="1" value={form.workspaceId} onChange={event => setForm({ ...form, workspaceId: event.target.value })}/></label><div className="form-pair"><label>Plan<select value={form.plan} onChange={event => setForm({ ...form, plan: event.target.value })}><option>starter</option><option>growth</option><option>business</option><option>enterprise</option></select></label><label>Seats<input type="number" min="1" max="10000" value={form.seats} onChange={event => setForm({ ...form, seats: event.target.value })}/></label></div><div className="form-pair"><label>Valid from<input type="date" value={form.validFrom} onChange={event => setForm({ ...form, validFrom: event.target.value })}/></label><label>Valid until<input type="date" value={form.validUntil} onChange={event => setForm({ ...form, validUntil: event.target.value })}/></label></div><label>Provider mode<select value={form.providerMode} onChange={event => setForm({ ...form, providerMode: event.target.value })}><option value="byok">BYOK only (chat)</option><option value="platform">Platform keys only</option><option value="hybrid">Hybrid (explicit per kind)</option></select></label>{form.providerMode === "hybrid" ? <div className="hybrid-grid">{(["Llm","Stt","Tts","Realtime","Telephony"] as const).map(kind => { const key = `hybrid${kind}` as keyof typeof form; return <label key={kind}>{kind}<select value={form[key]} disabled={kind === "Realtime"} onChange={event => setForm({ ...form, [key]: event.target.value })}>{kind !== "Realtime" ? <option value="byok">BYOK</option> : null}<option value="platform">Platform</option></select></label>; })}</div> : null}<p className="secure-note">This one-worker droplet uses its platform LiveKit transport. Hybrid tenants may still use BYOK for LLM, STT, TTS, and telephony.</p><div className="form-pair"><label>Voice seconds<input type="number" min="0" value={form.voiceSeconds} onChange={event => setForm({ ...form, voiceSeconds: event.target.value })}/></label><label>Token quota<input type="number" min="0" value={form.tokens} onChange={event => setForm({ ...form, tokens: event.target.value })}/></label></div><label>Agent limit<input type="number" min="1" value={form.agents} onChange={event => setForm({ ...form, agents: event.target.value })}/></label><button className="button" disabled={!form.workspaceId || !form.validFrom || !form.validUntil || form.validUntil <= form.validFrom} onClick={() => void issue()}>Issue signed license</button></section>
        <section className="settings-panel"><div className="panel-head"><div><small>ISSUED LICENSES</small><h2>Current authority records</h2></div><button className="mini-action" onClick={() => void load()}>Refresh</button></div>{loading ? <p>Loading…</p> : <div className="license-table"><div className="head"><span>Workspace</span><span>Plan</span><span>Validity</span><span>Status</span><span/></div>{rows.map(row => <div key={row.id}><span><b>{row.workspaceName || `Workspace ${row.workspaceId}`}</b><small>{row.seats} seats · {row.providerMode}</small></span><span>{row.plan}</span><span>{isoShort(row.validFrom)} — {isoShort(row.validUntil)}</span><em className={`license-${row.status}`}>{row.status}</em><button disabled={row.status === "revoked"} onClick={() => void revoke(row.id)}>Revoke</button></div>)}{!rows.length ? <p className="empty">No licenses issued.</p> : null}</div>}</section>
      </div>
      <section className="settings-panel settings-form" id="audio-review"><div className="panel-head"><div><small>RUNTIME AUDIO REVIEW</small><h2>{pendingAudio.length} pending recording{pendingAudio.length === 1 ? "" : "s"}</h2></div><button className="mini-action" type="button" onClick={() => void load()}>Refresh</button></div><p>Only a superadmin can approve tenant audio for published workflow playback. Listen to the complete file, record a review note, and approve only valid WAV audio.</p><div className="recording-list">{pendingAudio.map(row => { const canApprove = row.contentType === "audio/wav" || row.contentType === "audio/x-wav"; return <article key={row.id}><div><b>{row.filename}</b><small>Workspace {row.workspaceId} · {row.locale} · {formatBytes(row.size)}</small></div><audio controls preload="none" src={`/api/superadmin/audio-review/${row.id}/content`}/><label>Review note<input value={reviewNotes[row.id] || ""} maxLength={500} onChange={event => setReviewNotes(current => ({ ...current, [row.id]: event.target.value }))}/>{!canApprove ? <small>Non-WAV audio can only be rejected in this release.</small> : null}</label><div><button type="button" disabled={!canApprove || reviewingId === row.id} onClick={() => void reviewAudio(row.id, "approved")}>Approve</button><button type="button" disabled={reviewingId === row.id} onClick={() => void reviewAudio(row.id, "rejected")}>Reject</button></div></article>})}{!pendingAudio.length ? <p className="empty">No audio is waiting for review.</p> : null}</div></section>
    </div></section>
  </main>;
}

async function json<T>(response: Response): Promise<T> { const data = await response.json().catch(() => ({})) as T & { detail?: string; error?: string }; if (!response.ok) throw new Error(data.detail || data.error || `Request failed (${response.status})`); return data; }
function message(reason: unknown): string { return reason instanceof Error ? reason.message : "Request could not be completed"; }
function isoDay(value: Date): string { return value.toISOString().slice(0, 10); }
function isoShort(value: string): string { return value.slice(0, 10); }
function dayBoundary(value: string, endOfDay: boolean): string { return new Date(`${value}T${endOfDay ? "23:59:59.999" : "00:00:00.000"}Z`).toISOString(); }
function formatBytes(value: number): string { return value < 1024 ? `${value} B` : value < 1024 * 1024 ? `${Math.round(value / 1024)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`; }
