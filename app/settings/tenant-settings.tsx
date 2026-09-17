"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { formatDisplayDate } from "../../lib/format-date";
import type { AppUser, AppWorkspace } from "../../lib/server-api";

type License = {
  id?: number;
  status: string;
  plan: string;
  seats: number;
  seatsUsed?: number;
  validFrom: string;
  validUntil: string;
  providerMode: "byok" | "platform" | "hybrid";
  quotas?: Record<string, number | null>;
  usage?: Record<string, number>;
  features?: string[];
  hybridPolicy?: Record<string, string>;
};
type Provider = { id: number; kind: string; provider: string; label: string; status: string; updated_at?: string; has_secret?: boolean };
type Member = { id?: number; user_id?: number; name: string; email: string; role: string; status: string };
type Invite = { id: number; email: string; role: string; status?: string; expiresAt: string; acceptedAt?: string | null; revokedAt?: string | null };

const tabs = ["license", "providers", "members"] as const;
const providerCatalog: Record<string, string[]> = {
  llm: ["openai", "groq", "anthropic"],
  stt: ["deepgram", "elevenlabs", "openai"],
  tts: ["elevenlabs", "openai", "deepgram"],
};

export default function TenantSettings({ user, workspace, initialTab, licenseRequired }: { user: AppUser; workspace: AppWorkspace; initialTab?: string; licenseRequired: boolean }) {
  const [tab, setTab] = useState<(typeof tabs)[number]>(tabs.includes(initialTab as (typeof tabs)[number]) ? initialTab as (typeof tabs)[number] : "license");
  const [license, setLicense] = useState<License | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [notice, setNotice] = useState(licenseRequired ? "Activate or renew the workspace license to continue." : "");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [licenseKey, setLicenseKey] = useState("");
  const [invite, setInvite] = useState({ email: "", role: "member" });
  const [inviteToken, setInviteToken] = useState("");
  const [provider, setProvider] = useState({ kind: "llm", provider: "openai", label: "OpenAI", secret: "" });
  const [mountedAt] = useState(() => Date.now());
  const canAdmin = workspace.role === "owner" || workspace.role === "admin";

  const request = useCallback(async <T,>(path: string, options: RequestInit = {}): Promise<T> => {
    const response = await fetch(path, { ...options, headers: { ...(options.body instanceof FormData ? {} : { "content-type": "application/json" }), ...options.headers, "x-workspace-id": String(workspace.id) } });
    const data = await response.json().catch(() => ({})) as T & { detail?: string; error?: string };
    if (response.status === 401) window.location.assign(`/login?returnTo=${encodeURIComponent("/settings")}`);
    if (!response.ok) throw new Error(data.detail || data.error || `Request failed (${response.status})`);
    return data;
  }, [workspace.id]);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [licenseData, providerData, memberData, inviteData] = await Promise.all([
        request<{ license?: License | null }>("/api/licenses/current"),
        canAdmin ? request<{ connections?: Provider[] }>("/api/providers").catch(() => ({ connections: [] })) : Promise.resolve({ connections: [] }),
        canAdmin ? request<{ members?: Member[] }>("/api/members").catch(() => ({ members: [] })) : Promise.resolve({ members: [] }),
        canAdmin ? request<{ invites?: Invite[] }>("/api/invites").catch(() => ({ invites: [] })) : Promise.resolve({ invites: [] }),
      ]);
      setLicense(licenseData.license ? normalizeLicense(licenseData.license) : null);
      setProviders(providerData.connections || []);
      setMembers(memberData.members || []);
      setInvites(inviteData.invites || []);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Settings could not be loaded");
    } finally {
      setLoading(false);
    }
  }, [canAdmin, request]);

  useEffect(() => {
    const timer = window.setTimeout(() => void refresh(), 0);
    return () => window.clearTimeout(timer);
  }, [refresh]);

  async function activateLicense() {
    setError(""); setNotice("");
    try {
      const data = await request<{ license?: License }>("/api/licenses/activate", { method: "POST", body: JSON.stringify({ licenseKey }) });
      setLicenseKey("");
      if (data.license) setLicense(normalizeLicense(data.license));
      setNotice("License activated. Server-side access and quotas are now enabled.");
      await refresh();
    } catch (reason) { setError(message(reason)); }
  }

  async function saveProvider() {
    setError(""); setNotice("");
    try {
      if (!selectedKind || !selectedProvider) throw new Error("This license does not allow a tenant key for the selected provider kind");
      const data = await request<{ connection?: Provider }>("/api/providers", { method: "POST", body: JSON.stringify({ ...provider, kind: selectedKind, provider: selectedProvider, label: selectedLabel }) });
      setProvider(current => ({ ...current, kind: selectedKind, provider: selectedProvider, label: selectedLabel, secret: "" }));
      if (data.connection) setProviders(rows => [data.connection!, ...rows.filter(row => row.id !== data.connection!.id)]);
      setNotice("Provider credential encrypted and saved. The secret will not be shown again.");
    } catch (reason) { setError(message(reason)); }
  }

  async function disableProvider(id: number) {
    setError("");
    try {
      await request(`/api/providers/${id}`, { method: "DELETE" });
      setProviders(rows => rows.filter(row => row.id !== id));
      setNotice("Provider connection disabled.");
    } catch (reason) { setError(message(reason)); }
  }

  async function sendInvite() {
    setError(""); setNotice("");
    try {
      const data = await request<{ invite?: Invite; inviteToken?: string }>("/api/invites", { method: "POST", body: JSON.stringify(invite) });
      if (data.invite) setInvites(rows => [data.invite!, ...rows]);
      setInviteToken(data.inviteToken || "");
      setInvite(current => ({ ...current, email: "" }));
      setNotice("Invitation created. Seat limits are enforced by the server when it is accepted.");
    } catch (reason) { setError(message(reason)); }
  }

  const providerSource = license?.providerMode === "hybrid" ? JSON.stringify(license.hybridPolicy || {}) : license?.providerMode || "not licensed";
  const seatUsage = `${license?.seatsUsed ?? members.filter(row => row.status === "active").length} / ${license?.seats ?? "—"}`;
  const allowedKinds = Object.keys(providerCatalog).filter(kind => license?.providerMode === "byok" || (license?.providerMode === "hybrid" && license.hybridPolicy?.[kind] === "byok"));
  const selectedKind = allowedKinds.includes(provider.kind) ? provider.kind : allowedKinds[0] || "";
  const providerOptions = selectedKind ? providerCatalog[selectedKind] : [];
  const selectedProvider = providerOptions.includes(provider.provider) ? provider.provider : providerOptions[0] || "";
  const selectedLabel = provider.kind === selectedKind && provider.provider === selectedProvider ? provider.label : capitalize(selectedProvider);
  const allowedByok = Boolean(selectedKind && selectedProvider);
  const pendingInvites = invites.filter(row => !row.acceptedAt && !row.revokedAt && Date.parse(row.expiresAt) > mountedAt);
  const expiresSoon = useMemo(() => license ? Date.parse(license.validUntil) - mountedAt < 30 * 86400_000 : false, [license, mountedAt]);
  const activeKinds = new Set(providers.filter(row => row.status === "active").map(row => row.kind));
  const missingVoiceKinds = ["stt", "tts"].filter(kind => allowedKinds.includes(kind) && !activeKinds.has(kind));
  const voiceFallbackNotice = missingVoiceKinds.length
    ? `Tenant ${missingVoiceKinds.map(kind => kind.toUpperCase()).join(" and ")} ${missingVoiceKinds.length === 1 ? "is" : "are"} not configured. Voice sessions will use LiveKit Inference until you add Deepgram, ElevenLabs, or OpenAI keys here.`
    : "";

  return <main className="settings-shell">
    <aside className="settings-side">
      <Link className="brand" href="/"><span className="brand-mark">N</span><span>NEXORA</span></Link>
      <div className="workspace-chip"><small>WORKSPACE</small><b>{workspace.name}</b><span>{workspace.role}</span></div>
      <nav>{tabs.map(value => <button className={tab === value ? "on" : ""} key={value} onClick={() => setTab(value)}>{value === "license" ? "◆" : value === "providers" ? "⌘" : "◎"}<span>{capitalize(value)}</span></button>)}</nav>
      <div className="settings-account"><b>{user.name}</b><small>{user.email}</small><Link href="/dashboard">Dashboard</Link><Link href="/logout">Sign out</Link></div>
    </aside>
    <section className="settings-main">
      <header><div><small>TENANT ADMINISTRATION</small><h1>{capitalize(tab)}</h1></div>{user.isSuperadmin ? <Link className="ghost-button" href="/superadmin/licenses">Superadmin</Link> : null}</header>
      <div className="settings-content">
        {notice ? <p className="settings-notice" role="status">{notice}</p> : null}
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        {loading ? <p className="settings-loading">Loading secure workspace settings…</p> : null}

        {!loading && tab === "license" ? <>
          <section className="settings-grid license-summary">
            <article><small>STATUS</small><b className={`license-${license?.status || "unused"}`}>{license?.status || "Not activated"}</b></article>
            <article><small>PLAN</small><b>{license?.plan || "—"}</b></article>
            <article><small>SEATS</small><b>{seatUsage}</b></article>
            <article><small>PROVIDER MODE</small><b>{license?.providerMode || "—"}</b></article>
          </section>
          {license ? <section className="settings-panel"><div className="panel-head"><div><small>LICENSE WINDOW</small><h2>{formatDisplayDate(license.validFrom)} — {formatDisplayDate(license.validUntil)}</h2></div>{expiresSoon ? <span className="license-warning">Renew soon</span> : null}</div><p>Provider policy: <code>{providerSource}</code>. This policy is verified and enforced by FastAPI on every tenant request; changing browser data does not change access.</p><div className="quota-grid">{Object.entries(license.quotas || {}).map(([key, value]) => <span key={key}><small>{key.replaceAll("_", " ")}</small><b>{license.usage?.[key] || 0} / {value ?? "unlimited"}</b></span>)}</div></section> : null}
          {canAdmin ? <section className="settings-panel settings-form"><small>ACTIVATE OR REPLACE</small><h2>Paste a signed license</h2><p>The license key is verified against the platform Ed25519 public key and is never shown again after activation.</p><label>License key<textarea value={licenseKey} onChange={event => setLicenseKey(event.target.value)} autoComplete="off" spellCheck={false}/></label><button className="button" disabled={licenseKey.trim().length < 40} onClick={() => void activateLicense()}>Activate license</button></section> : null}
        </> : null}

        {!loading && tab === "providers" ? canAdmin ? <>
          {voiceFallbackNotice ? <p className="settings-notice" role="status">{voiceFallbackNotice}</p> : null}
          <div className="settings-columns">
          <section className="settings-panel"><small>CONNECTION VAULT</small><h2>Tenant provider keys</h2><p>Only metadata is returned. Secret values are AES-GCM encrypted by the Python API and never sent back to this page.</p><div className="settings-list">{providers.map(row => <div key={row.id}><span>●</span><p><b>{row.label}</b><small>{row.kind} · {row.provider} · {row.status}</small></p><button onClick={() => void disableProvider(row.id)}>Disable</button></div>)}{!providers.length ? <p className="empty">No tenant keys stored.</p> : null}</div></section>
          <section className="settings-panel settings-form"><small>PROVIDER POLICY</small><h2>{license?.providerMode || "No active license"}</h2><p>{license?.providerMode === "platform" ? "This license uses platform credentials. Tenant inference keys cannot be added." : license?.providerMode === "hybrid" ? "The kind selector shows only inference kinds explicitly marked BYOK in the signed hybrid policy." : "This workspace must supply its own inference credentials. Platform keys are never used as a fallback."}</p><p className="secure-note">Shared-worker LiveKit credentials are platform-managed and are not accepted here. Configure licensed BYOK carrier credentials on the Telephony page.</p><label>Kind<select value={selectedKind} onChange={event => { const kind = event.target.value; const nextProvider = providerCatalog[kind]?.[0] || ""; setProvider({ ...provider, kind, provider: nextProvider, label: capitalize(nextProvider) }); }} disabled={!allowedKinds.length}>{allowedKinds.length ? allowedKinds.map(value => <option key={value}>{value}</option>) : <option value="">No BYOK inference kind licensed</option>}</select></label><label>Provider<select value={selectedProvider} onChange={event => setProvider({ ...provider, kind: selectedKind, provider: event.target.value, label: capitalize(event.target.value) })} disabled={!allowedByok}>{providerOptions.map(value => <option key={value}>{value}</option>)}</select></label><label>Label<input value={selectedLabel} maxLength={80} onChange={event => setProvider({ ...provider, kind: selectedKind, provider: selectedProvider, label: event.target.value })} disabled={!allowedByok}/></label><label>Secret key<input type="password" autoComplete="new-password" value={provider.secret} onChange={event => setProvider({ ...provider, secret: event.target.value })} disabled={!allowedByok}/></label><button className="button" type="button" disabled={!allowedByok || provider.secret.length < 8} onClick={() => void saveProvider()}>Encrypt and save</button></section>
          </div>
        </> : <p className="settings-loading">Owner or admin role is required to manage provider credentials.</p> : null}

        {!loading && tab === "members" ? canAdmin ? <div className="settings-columns">
          <section className="settings-panel"><div className="panel-head"><div><small>ACTIVE MEMBERS</small><h2>{seatUsage} seats used</h2></div></div><div className="settings-list">{members.map((row, index) => <div key={row.id || row.user_id || `${row.email}-${index}`}><span>{row.name.slice(0, 1).toUpperCase()}</span><p><b>{row.name}</b><small>{row.email} · {row.role}</small></p><em>{row.status}</em></div>)}</div><h3>Pending invitations</h3><div className="settings-list">{pendingInvites.map(row => <div key={row.id}><span>✉</span><p><b>{row.email}</b><small>{row.role} · expires {formatDisplayDate(row.expiresAt)}</small></p></div>)}{!pendingInvites.length ? <p className="empty">No pending invitations.</p> : null}</div></section>
          <section className="settings-panel settings-form"><small>INVITE USER</small><h2>Add a workspace member</h2><p>The invite token is hashed, email-bound, and expires. Licensed seats are reserved when an invitation is created and checked again when it is accepted.</p>{inviteToken ? <div className="one-time-license"><small>SHOWN ONCE</small><h3>Copy the invitation link</h3><code>/register?invite={inviteToken}</code><button className="mini-action" type="button" onClick={() => void navigator.clipboard.writeText(`${window.location.origin}/register?invite=${encodeURIComponent(inviteToken)}`)}>Copy invite link</button></div> : null}<label>Email<input type="email" autoComplete="off" value={invite.email} onChange={event => setInvite({ ...invite, email: event.target.value })}/></label><label>Role<select value={invite.role} onChange={event => setInvite({ ...invite, role: event.target.value })}><option>viewer</option><option>member</option><option>operator</option><option>admin</option></select></label><button className="button" type="button" disabled={!/^\S+@\S+\.\S+$/.test(invite.email)} onClick={() => void sendInvite()}>Create invitation</button></section>
        </div> : <p className="settings-loading">Owner or admin role is required to manage members.</p> : null}
      </div>
    </section>
  </main>;
}

function message(reason: unknown): string { return reason instanceof Error ? reason.message : "Request could not be completed"; }
function capitalize(value: string): string { return value.slice(0, 1).toUpperCase() + value.slice(1); }
function normalizeLicense(value: License & Record<string, unknown>): License { return { ...value, validFrom: String(value.validFrom || value.valid_from || ""), validUntil: String(value.validUntil || value.valid_until || ""), providerMode: String(value.providerMode || value.provider_mode || "byok") as License["providerMode"], seatsUsed: Number(value.seatsUsed ?? value.seats_used ?? 0), hybridPolicy: (value.hybridPolicy || value.hybrid_policy || {}) as Record<string,string> }; }
