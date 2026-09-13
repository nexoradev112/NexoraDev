"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

type Workspace = {
  id: number;
  name: string;
  slug: string;
  plan: string;
  status: string;
  region: string;
  retentionDays: number;
  role: string;
};

type Agent = {
  id: number;
  name: string;
  locale: string;
  channel: string;
  status: string;
  updatedAt: string;
};

type Call = {
  id: number;
  agentId: number;
  campaignId: number | null;
  direction: string;
  status: string;
  durationSeconds: number;
  summary: string | null;
  sentiment: string | null;
  costMicros: number;
  createdAt: string;
};

type Campaign = {
  id: number;
  agentId: number;
  name: string;
  status: string;
  audienceCount: number;
  dataSourceType: string;
  maxConcurrentCalls: number;
  retryPolicy: {
    enabled: boolean;
    maxRetries: number;
    delaySeconds: number;
    retryOn: string[];
  };
  schedulePolicy: {
    timezone: string;
    startHour: number;
    endHour: number;
    weekdays: number[];
  };
  circuitBreaker: {
    enabled: boolean;
    failureThreshold: number;
    windowSeconds: number;
    minCalls: number;
  };
  scheduledAt: string | null;
  updatedAt: string;
};

type Integration = {
  id: number;
  name: string;
  kind: string;
  baseUrl: string;
  authType: string;
  status: string;
  updatedAt: string;
};

type Phone = {
  id: number;
  provider: string;
  e164: string;
  label: string;
  direction: string;
  status: string;
};

type Suppression = {
  id: number;
  reason: string;
  source: string;
  expiresAt: string | null;
  createdAt: string;
};

type Consent = {
  id: number;
  channel?: "voice" | "recording";
  status: string;
  legalBasis: string;
  evidenceRef: string;
  capturedAt: string;
  expiresAt: string | null;
};

type Reservation = {
  id: string;
  campaignId: number | null;
  quantity: number;
  consumed: number;
  status: string;
  expiresAt: string;
};

type Subscription = {
  status: string;
  priceRef: string;
  currentPeriodEnd: string | null;
  cancelAtPeriodEnd: boolean;
};

type OperationsConsoleProps = {
  userName: string;
  brand: string;
  workspace: Workspace;
  canOperate: boolean;
  canAdmin: boolean;
  agents: Agent[];
  calls: Call[];
  campaigns: Campaign[];
  integrations: Integration[];
  phoneNumbers: Phone[];
  providers: Array<{
    id: number;
    kind: string;
    provider: string;
    label: string;
    status: string;
  }>;
  usage: Array<{ provider: string; type: string; quantity: number }>;
  audit: Array<{
    id: number;
    actorId: string;
    action: string;
    resourceType: string;
    resourceId: string;
    createdAt: string;
  }>;
  members: Array<{ name: string; email: string; role: string; status: string }>;
  suppressions: Suppression[];
  consents: Consent[];
  reservations: Reservation[];
  subscription: Subscription | null;
};

type IntegrationForm = {
  name: string;
  kind: string;
  baseUrl: string;
  authType: string;
  secret: string;
};

const TELEPHONY_PROVIDERS = [
  "twilio",
  "vonage",
  "vobiz",
  "convox",
  "telnyx",
  "cloudonix",
  "asterisk",
] as const;

type TelephonyProvider = (typeof TELEPHONY_PROVIDERS)[number];

type PhoneForm = {
  provider: TelephonyProvider;
  e164: string;
  label: string;
  direction: "both" | "inbound" | "outbound";
};

type ComplianceForm = {
  type: "consent" | "suppression";
  phoneNumber: string;
  channel: "voice" | "recording";
  status: "granted" | "revoked";
  legalBasis: "consent" | "contract" | "legitimate-interest";
  evidenceRef: string;
  reason: "do-not-call" | "complaint" | "legal" | "manual";
};

type Notice = { kind: "success" | "error"; text: string } | null;

export default function OperationsConsole(props: OperationsConsoleProps) {
  const [integrations, setIntegrations] = useState(props.integrations);
  const [phones, setPhones] = useState(props.phoneNumbers);
  const [consents, setConsents] = useState(props.consents);
  const [suppressions, setSuppressions] = useState(props.suppressions);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [palette, setPalette] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [pending, setPending] = useState<"integration" | "phone" | "compliance" | null>(
    null,
  );
  const [integrationForm, setIntegrationForm] = useState<IntegrationForm>({
    name: "",
    kind: "webhook",
    baseUrl: "",
    authType: "bearer",
    secret: "",
  });
  const [phoneForm, setPhoneForm] = useState<PhoneForm>({
    provider: "twilio",
    e164: "",
    label: "Main line",
    direction: "both",
  });
  const [complianceForm, setComplianceForm] = useState<ComplianceForm>({
    type: "consent",
    phoneNumber: "",
    channel: "voice",
    status: "granted",
    legalBasis: "consent",
    evidenceRef: "",
    reason: "do-not-call",
  });

  const agentMap = useMemo(
    () => new Map(props.agents.map((agent) => [agent.id, agent.name])),
    [props.agents],
  );
  const filteredCalls = useMemo(() => {
    const term = search.trim().toLowerCase();
    return props.calls.filter((call) => {
      const matchesStatus = status === "all" || call.status === status;
      const searchable = `${call.id} ${agentMap.get(call.agentId) || ""} ${call.summary || ""} ${call.sentiment || ""}`.toLowerCase();
      return matchesStatus && searchable.includes(term);
    });
  }, [props.calls, status, search, agentMap]);

  const totalSeconds = props.calls.reduce((sum, row) => sum + row.durationSeconds, 0);
  const totalCost =
    props.calls.reduce((sum, row) => sum + row.costMicros, 0) / 1_000_000;

  async function saveIntegration() {
    setPending("integration");
    setNotice(null);
    try {
      const data = await api<{ integration?: Integration }>(
        "/api/integrations",
        props.workspace.id,
        integrationForm,
      );
      if (data.integration) {
        setIntegrations((rows) => [
          data.integration!,
          ...rows.filter((row) => row.id !== data.integration!.id),
        ]);
      }
      setIntegrationForm((current) => ({ ...current, secret: "" }));
      setNotice({
        kind: "success",
        text: "Integration credential encrypted and saved for this workspace.",
      });
    } catch (error) {
      setNotice({ kind: "error", text: errorMessage(error) });
    } finally {
      setPending(null);
    }
  }

  async function savePhone() {
    setPending("phone");
    setNotice(null);
    try {
      const data = await api<{ phoneNumber?: Phone; number?: Phone }>(
        "/api/phone-numbers",
        props.workspace.id,
        phoneForm,
      );
      const savedPhone = data.phoneNumber || data.number;
      if (savedPhone) {
        setPhones((rows) => [savedPhone, ...rows.filter((row) => row.id !== savedPhone.id)]);
      }
      setPhoneForm((current) => ({ ...current, e164: "" }));
      setNotice({ kind: "success", text: "Phone number saved for this workspace." });
    } catch (error) {
      setNotice({ kind: "error", text: errorMessage(error) });
    } finally {
      setPending(null);
    }
  }

  async function saveCompliance() {
    setPending("compliance");
    setNotice(null);
    try {
      const data = await api<{ consent?: Consent; suppression?: Suppression }>(
        "/api/compliance",
        props.workspace.id,
        complianceForm,
      );
      if (data.consent) {
        setConsents((rows) => [
          data.consent!,
          ...rows.filter((row) => row.id !== data.consent!.id),
        ]);
      }
      if (data.suppression) {
        setSuppressions((rows) => [
          data.suppression!,
          ...rows.filter((row) => row.id !== data.suppression!.id),
        ]);
      }
      setComplianceForm((current) => ({
        ...current,
        phoneNumber: "",
        evidenceRef: "",
      }));
      setNotice({
        kind: "success",
        text:
          complianceForm.type === "consent"
            ? `${titleCase(complianceForm.channel)} consent evidence recorded without storing the phone number in plaintext.`
            : "Phone fingerprint added to the suppression list.",
      });
    } catch (error) {
      setNotice({ kind: "error", text: errorMessage(error) });
    } finally {
      setPending(null);
    }
  }

  function exportCalls() {
    const rows = [
      "call_id,agent,direction,status,duration_seconds,sentiment,cost_usd,created_at",
      ...filteredCalls.map((call) =>
        [
          call.id,
          csvCell(agentMap.get(call.agentId) || String(call.agentId)),
          csvCell(call.direction),
          csvCell(call.status),
          call.durationSeconds,
          csvCell(call.sentiment || ""),
          (call.costMicros / 1_000_000).toFixed(4),
          csvCell(call.createdAt),
        ].join(","),
      ),
    ];
    downloadText(
      `calls-${new Date().toISOString().slice(0, 10)}.csv`,
      rows.join("\n"),
      "text/csv",
    );
  }

  function exportAudit() {
    const rows = [
      "created_at,actor,action,resource_type,resource_id",
      ...props.audit.map((event) =>
        [
          csvCell(event.createdAt),
          csvCell(event.actorId),
          csvCell(event.action),
          csvCell(event.resourceType),
          csvCell(event.resourceId),
        ].join(","),
      ),
    ];
    downloadText(
      `audit-${new Date().toISOString().slice(0, 10)}.csv`,
      rows.join("\n"),
      "text/csv",
    );
  }

  return (
    <main className="ops-shell operations-console">
      <aside className="ops-side">
        <Link className="brand" href="/">
          <span className="brand-mark">{props.brand.slice(0, 1).toUpperCase()}</span>
          <span>{props.brand.toUpperCase()}</span>
        </Link>
        <div className="workspace-chip">
          <small>WORKSPACE</small>
          <b>{props.workspace.name}</b>
          <span>
            {props.workspace.plan} · {props.workspace.role}
          </span>
        </div>
        <nav>
          <Link href="/dashboard">
            ⌂ <span>Overview</span>
          </Link>
          <Link href="/agents">
            ✦ <span>Agents</span>
          </Link>
          <a className="on" href="#observe">
            ◉ <span>Observe</span>
          </a>
          <Link href="/analytics">
            ◌ <span>Analytics</span>
          </Link>
          <Link href="/reports">
            ▤ <span>Reports</span>
          </Link>
          <a href="#campaigns">
            ◫ <span>Campaigns</span>
          </a>
          <Link href="/recordings">
            ♫ <span>Audio</span>
          </Link>
          <Link href="/telephony">
            ☎ <span>Telephony</span>
          </Link>
          <a href="#compliance">
            ✓ <span>Compliance</span>
          </a>
          <a href="#admin">
            ⚿ <span>Admin</span>
          </a>
        </nav>
        <div className="ops-user">
          <span>{props.userName.slice(0, 1).toUpperCase()}</span>
          <div>
            <b>{props.userName}</b>
            <small>{props.workspace.status}</small>
          </div>
        </div>
      </aside>

      <section className="ops-main">
        <header>
          <div>
            <small>OPERATIONS CONTROL PLANE</small>
            <h1>Run, observe, and govern.</h1>
            <p>Call intelligence, integrations, telephony, usage, and tenant controls.</p>
          </div>
          <div>
            <button
              className="ghost-button palette-trigger"
              type="button"
              onClick={() => setPalette(true)}
            >
              Jump to anything <kbd>⌘ K</kbd>
            </button>
            <Link className="button small" href="/studio?new=1">
              ＋ New agent
            </Link>
          </div>
        </header>

        <div className="ops-content" id="observe">
          {notice ? (
            <p className="ops-notice" role={notice.kind === "error" ? "alert" : "status"}>
              {notice.text}
              <button type="button" onClick={() => setNotice(null)} aria-label="Dismiss notice">
                ×
              </button>
            </p>
          ) : null}

          <section className="ops-metrics">
            <article>
              <span>◉</span>
              <small>Calls</small>
              <b>{props.calls.length}</b>
              <em>Filtered workspace records</em>
            </article>
            <article>
              <span>◷</span>
              <small>Talk time</small>
              <b>{Math.round(totalSeconds / 60)}m</b>
              <em>Across voice sessions</em>
            </article>
            <article>
              <span>＄</span>
              <small>Usage cost</small>
              <b>${totalCost.toFixed(2)}</b>
              <em>Provider-attributed</em>
            </article>
            <article>
              <span>◫</span>
              <small>Campaigns</small>
              <b>{props.campaigns.length}</b>
              <em>Read-only preview · Phase 2</em>
            </article>
          </section>

          <section className="ops-panel operations-section">
            <div className="panel-head">
              <div>
                <small>OBSERVE · CALL LOGS</small>
                <h2>Every conversation, searchable</h2>
              </div>
              <button className="mini-action" type="button" onClick={exportCalls}>
                Export CSV
              </button>
            </div>
            <div className="filter-bar">
              <input
                aria-label="Search calls"
                placeholder="Search ID, agent, summary, sentiment…"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
              <select
                aria-label="Filter call status"
                value={status}
                onChange={(event) => setStatus(event.target.value)}
              >
                <option value="all">All statuses</option>
                <option value="queued">Queued</option>
                <option value="active">Active</option>
                <option value="in-progress">In progress</option>
                <option value="completed">Completed</option>
                <option value="failed">Failed</option>
              </select>
            </div>
            <div className="call-table">
              <div className="call-row head">
                <span>Call</span>
                <span>Agent</span>
                <span>Status</span>
                <span>Duration</span>
                <span>Outcome</span>
              </div>
              {filteredCalls.length ? (
                filteredCalls.map((call) => (
                  <div className="call-row" key={call.id}>
                    <b>#{call.id}</b>
                    <span>{agentMap.get(call.agentId) || `Agent ${call.agentId}`}</span>
                    <em className={`call-${call.status}`}>{call.status}</em>
                    <span>{formatDuration(call.durationSeconds)}</span>
                    <span>{call.summary || call.sentiment || "Awaiting result"}</span>
                  </div>
                ))
              ) : (
                <p className="empty">No calls match these filters.</p>
              )}
            </div>
          </section>

          <section className="ops-grid" id="campaigns">
            <article className="ops-panel">
              <small>RUN · CAMPAIGNS · PHASE 2</small>
              <h2>Outbound campaign history</h2>
              <p className="secure-note">
                Campaign creation, contact upload, dispatch, retries, and circuit breakers are not
                enabled in this droplet release. Existing imported records are read-only.
              </p>
              <div className="compact-list">
                {props.campaigns.length ? (
                  props.campaigns.map((row) => (
                    <p key={row.id}>
                      <span>◫</span>
                      <b>
                        {row.name}
                        <small>
                          {agentMap.get(row.agentId) || `Agent ${row.agentId}`} · {row.audienceCount}{" "}
                          contacts · max {row.maxConcurrentCalls} concurrent
                        </small>
                      </b>
                      <em>{row.status}</em>
                    </p>
                  ))
                ) : (
                  <p className="empty">No imported campaign records.</p>
                )}
              </div>
            </article>
            <article className="ops-panel operations-form">
              <small>PHASE 2</small>
              <h2>Campaign dispatch is reserved</h2>
              <p className="secure-note">
                This control stays disabled until the dispatcher has quota reservations, consent
                enforcement, quiet hours, and failure isolation wired end to end.
              </p>
              <button className="button" type="button" disabled>
                Campaigns available in Phase 2
              </button>
            </article>
          </section>

          <section className="ops-grid" id="connections">
            <article className="ops-panel">
              <small>BUILD · CONNECTIONS</small>
              <h2>Providers, tools, and phone numbers</h2>
              <div className="connection-counters">
                <span>
                  <b>{props.providers.length}</b> AI providers
                </span>
                <span>
                  <b>{integrations.length}</b> integrations
                </span>
                <span>
                  <b>{phones.length}</b> phone numbers
                </span>
              </div>
              <h3>Integration vault</h3>
              <div className="compact-list">
                {integrations.map((row) => (
                  <p key={row.id}>
                    <span>◇</span>
                    <b>
                      {row.name}
                      <small>
                        {row.kind} · {hostFromUrl(row.baseUrl)}
                      </small>
                    </b>
                    <em>{row.status}</em>
                  </p>
                ))}
                {!integrations.length ? <p className="empty">No integrations connected.</p> : null}
              </div>
              <h3>Telephony</h3>
              <div className="compact-list">
                {phones.map((row) => (
                  <p key={row.id}>
                    <span>☎</span>
                    <b>
                      {row.label}
                      <small>
                        {maskPhone(row.e164)} · {row.provider}
                      </small>
                    </b>
                    <em>{row.direction}</em>
                  </p>
                ))}
                {!phones.length ? <p className="empty">No phone numbers configured.</p> : null}
              </div>
            </article>

            {props.canAdmin ? (
              <article className="ops-panel operations-form">
                <small>SECURE CONNECTION</small>
                <h2>Add integration</h2>
                <label>
                  Name
                  <input
                    value={integrationForm.name}
                    onChange={(event) =>
                      setIntegrationForm((current) => ({
                        ...current,
                        name: event.target.value,
                      }))
                    }
                  />
                </label>
                <label>
                  Type
                  <select
                    value={integrationForm.kind}
                    onChange={(event) =>
                      setIntegrationForm((current) => ({
                        ...current,
                        kind: event.target.value,
                      }))
                    }
                  >
                    <option value="webhook">Webhook</option>
                    <option value="crm">CRM</option>
                    <option value="calendar">Calendar</option>
                    <option value="database">Database</option>
                    <option value="custom-api">Custom API</option>
                  </select>
                </label>
                <label>
                  HTTPS base URL
                  <input
                    type="url"
                    placeholder="https://api.example.com"
                    value={integrationForm.baseUrl}
                    onChange={(event) =>
                      setIntegrationForm((current) => ({
                        ...current,
                        baseUrl: event.target.value,
                      }))
                    }
                  />
                </label>
                <label>
                  Credential
                  <input
                    type="password"
                    autoComplete="new-password"
                    value={integrationForm.secret}
                    onChange={(event) =>
                      setIntegrationForm((current) => ({
                        ...current,
                        secret: event.target.value,
                      }))
                    }
                  />
                </label>
                <p className="secure-note">
                  Credentials are encrypted server-side and are never returned by the API.
                </p>
                <button
                  className="button"
                  type="button"
                  disabled={
                    pending !== null ||
                    !integrationForm.name ||
                    !integrationForm.baseUrl.startsWith("https://") ||
                    integrationForm.secret.length < 8
                  }
                  onClick={() => void saveIntegration()}
                >
                  {pending === "integration" ? "Saving…" : "Save integration"}
                </button>

                <h3>Add phone number</h3>
                <div className="form-pair">
                  <label>
                    Provider
                    <select
                      value={phoneForm.provider}
                      onChange={(event) =>
                        setPhoneForm((current) => ({
                          ...current,
                          provider: event.target.value as TelephonyProvider,
                        }))
                      }
                    >
                      {TELEPHONY_PROVIDERS.map((provider) => (
                        <option key={provider} value={provider}>
                          {titleCase(provider)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Direction
                    <select
                      value={phoneForm.direction}
                      onChange={(event) =>
                        setPhoneForm((current) => ({
                          ...current,
                          direction: event.target.value as PhoneForm["direction"],
                        }))
                      }
                    >
                      <option value="both">Both</option>
                      <option value="inbound">Inbound</option>
                      <option value="outbound">Outbound</option>
                    </select>
                  </label>
                </div>
                <label>
                  Number
                  <input
                    placeholder="+14155550123"
                    value={phoneForm.e164}
                    onChange={(event) =>
                      setPhoneForm((current) => ({ ...current, e164: event.target.value }))
                    }
                  />
                </label>
                <button
                  className="mini-action"
                  type="button"
                  disabled={pending !== null || !/^\+[1-9]\d{7,14}$/.test(phoneForm.e164)}
                  onClick={() => void savePhone()}
                >
                  {pending === "phone" ? "Saving…" : "Save phone number"}
                </button>
              </article>
            ) : (
              <article className="ops-panel">
                <small>ROLE BOUNDARY</small>
                <h2>Tenant admin access required</h2>
                <p className="secure-note">
                  Your role can view connection inventory but cannot add credentials or numbers.
                </p>
              </article>
            )}
          </section>

          {props.canAdmin ? (
            <section className="ops-grid" id="compliance">
              <article className="ops-panel">
                <small>GOVERN · COMPLIANCE</small>
                <h2>Consent, suppression, and reservations</h2>
                <div className="connection-counters">
                  <span>
                    <b>{consents.length}</b> consent events
                  </span>
                  <span>
                    <b>{suppressions.length}</b> suppressed contacts
                  </span>
                  <span>
                    <b>
                      {props.reservations.filter((row) => row.status === "reserved").length}
                    </b>{" "}
                    active reservations
                  </span>
                </div>
                <p className="secure-note">
                  Phone numbers entered here are normalized, not stored in plaintext, and
                  represented only by workspace-scoped fingerprints.
                </p>
                <h3>Recent evidence</h3>
                <div className="compact-list">
                  {consents.slice(0, 4).map((row) => (
                    <p key={row.id}>
                      <span>✓</span>
                      <b>
                        {row.evidenceRef}
                        <small>
                          {titleCase(row.channel || "voice")} · {row.legalBasis} ·{" "}
                          {new Date(row.capturedAt).toLocaleString()}
                        </small>
                      </b>
                      <em>{row.status}</em>
                    </p>
                  ))}
                  {!consents.length ? <p className="empty">No consent evidence yet.</p> : null}
                </div>
              </article>

              <article className="ops-panel operations-form">
                <small>CONTACT POLICY</small>
                <h2>Add compliance evidence</h2>
                <label>
                  Record type
                  <select
                    value={complianceForm.type}
                    onChange={(event) =>
                      setComplianceForm((current) => ({
                        ...current,
                        type: event.target.value as ComplianceForm["type"],
                      }))
                    }
                  >
                    <option value="consent">Consent evidence</option>
                    <option value="suppression">Suppression / do not call</option>
                  </select>
                </label>
                <label>
                  Phone number
                  <input
                    placeholder="+14155550123"
                    value={complianceForm.phoneNumber}
                    onChange={(event) =>
                      setComplianceForm((current) => ({
                        ...current,
                        phoneNumber: event.target.value,
                      }))
                    }
                  />
                </label>

                {complianceForm.type === "consent" ? (
                  <>
                    <label>
                      Consent channel
                      <select
                        value={complianceForm.channel}
                        onChange={(event) =>
                          setComplianceForm((current) => ({
                            ...current,
                            channel: event.target.value as ComplianceForm["channel"],
                          }))
                        }
                      >
                        <option value="voice">Voice contact</option>
                        <option value="recording">Call recording</option>
                      </select>
                    </label>
                    <div className="form-pair">
                      <label>
                        Status
                        <select
                          value={complianceForm.status}
                          onChange={(event) =>
                            setComplianceForm((current) => ({
                              ...current,
                              status: event.target.value as ComplianceForm["status"],
                            }))
                          }
                        >
                          <option value="granted">Granted</option>
                          <option value="revoked">Revoked</option>
                        </select>
                      </label>
                      <label>
                        Legal basis
                        <select
                          value={complianceForm.legalBasis}
                          onChange={(event) =>
                            setComplianceForm((current) => ({
                              ...current,
                              legalBasis: event.target.value as ComplianceForm["legalBasis"],
                            }))
                          }
                        >
                          <option value="consent">Consent</option>
                          <option value="contract">Contract</option>
                          <option value="legitimate-interest">Legitimate interest</option>
                        </select>
                      </label>
                    </div>
                    <label>
                      Evidence reference
                      <input
                        placeholder="crm:contact/123/consent"
                        value={complianceForm.evidenceRef}
                        onChange={(event) =>
                          setComplianceForm((current) => ({
                            ...current,
                            evidenceRef: event.target.value,
                          }))
                        }
                      />
                    </label>
                  </>
                ) : (
                  <label>
                    Reason
                    <select
                      value={complianceForm.reason}
                      onChange={(event) =>
                        setComplianceForm((current) => ({
                          ...current,
                          reason: event.target.value as ComplianceForm["reason"],
                        }))
                      }
                    >
                      <option value="do-not-call">Do not call</option>
                      <option value="complaint">Complaint</option>
                      <option value="legal">Legal hold</option>
                      <option value="manual">Manual</option>
                    </select>
                  </label>
                )}
                <button
                  className="button"
                  type="button"
                  disabled={
                    pending !== null ||
                    !/^\+[1-9]\d{7,14}$/.test(complianceForm.phoneNumber) ||
                    (complianceForm.type === "consent" && complianceForm.evidenceRef.length < 3)
                  }
                  onClick={() => void saveCompliance()}
                >
                  {pending === "compliance" ? "Saving…" : "Save compliance record"}
                </button>

                <h3>Telemetry · Phase 2</h3>
                <p className="secure-note">
                  Langfuse and OpenTelemetry credential controls remain disabled until the Python
                  runtime exports tenant-scoped traces with redaction.
                </p>
                <button className="mini-action" type="button" disabled>
                  Telemetry available in Phase 2
                </button>
              </article>
            </section>
          ) : null}

          <section className="ops-grid" id="admin">
            <article className="ops-panel">
              <small>ADMIN · GOVERNANCE</small>
              <h2>Members and audit</h2>
              <div className="compact-list">
                {props.members.map((row) => (
                  <p key={row.email}>
                    <span>{row.name?.slice(0, 1).toUpperCase() || "U"}</span>
                    <b>
                      {row.name || row.email}
                      <small>{row.email}</small>
                    </b>
                    <em>{row.role}</em>
                  </p>
                ))}
                {!props.members.length ? <p className="empty">No members to display.</p> : null}
              </div>
              <h3>Recent audit events</h3>
              <div className="audit-stream">
                {props.audit.map((row) => (
                  <p key={row.id}>
                    <time>{new Date(row.createdAt).toLocaleString()}</time>
                    <b>{row.action}</b>
                    <span>
                      {row.resourceType} {row.resourceId}
                    </span>
                  </p>
                ))}
                {!props.audit.length ? <p className="empty">No audit events to display.</p> : null}
              </div>
              <button
                className="mini-action"
                type="button"
                onClick={exportAudit}
                disabled={!props.audit.length}
              >
                Export loaded audit events
              </button>
            </article>

            <article className="ops-panel operations-form">
              <small>DATA RESIDENCY · PHASE 2</small>
              <h2>Workspace policy</h2>
              <p className="secure-note">
                One-droplet storage uses the deployment region and operator-managed backups.
                Per-tenant region and automated retention controls are planned for Phase 2.
              </p>
              <label>
                Name
                <input value={props.workspace.name} readOnly disabled />
              </label>
              <label>
                Region
                <select value={props.workspace.region} disabled onChange={() => undefined}>
                  <option value={props.workspace.region}>{props.workspace.region}</option>
                </select>
              </label>
              <label>
                Retention
                <select
                  value={String(props.workspace.retentionDays)}
                  disabled
                  onChange={() => undefined}
                >
                  <option value={String(props.workspace.retentionDays)}>
                    {props.workspace.retentionDays} days
                  </option>
                </select>
              </label>
              <button className="button" type="button" disabled>
                Policy controls available in Phase 2
              </button>

              <h3>Billing · Phase 2</h3>
              <p className="secure-note">
                <b>{props.workspace.plan}</b> · licensed access
                {props.subscription?.currentPeriodEnd
                  ? ` · recorded period ends ${new Date(props.subscription.currentPeriodEnd).toLocaleDateString()}`
                  : ""}
              </p>
              <button className="mini-action" type="button" disabled>
                Stripe portal available in Phase 2
              </button>

              <h3>Agent portability</h3>
              <p className="secure-note">
                Export tenant-scoped definitions without credentials. Imported definitions always
                enter another workspace as drafts.
              </p>
              {props.agents.map((agent) => (
                <p className="export-line" key={agent.id}>
                  <b>{agent.name}</b>
                  <a href={`/api/agents/definition?id=${agent.id}&workspace=${props.workspace.id}`}>
                    Export JSON
                  </a>
                </p>
              ))}
            </article>
          </section>
        </div>
      </section>

      {palette ? (
        <div
          className="command-overlay"
          role="dialog"
          aria-modal="true"
          aria-label="Jump to anything"
          onClick={() => setPalette(false)}
        >
          <div onClick={(event) => event.stopPropagation()}>
            <div>
              <b>Jump to anything</b>
              <button type="button" onClick={() => setPalette(false)} aria-label="Close">
                ×
              </button>
            </div>
            {[
              ["Overview", "/dashboard"],
              ["Agent builder", "/studio"],
              ["Call logs", "#observe"],
              ["Campaigns · Phase 2", "#campaigns"],
              ["Connections", "#connections"],
              ["Compliance", "#compliance"],
              ["Workspace admin", "#admin"],
              ["CMS", "/admin"],
            ].map((item) => (
              <a key={item[0]} href={item[1]} onClick={() => setPalette(false)}>
                <span>↗</span>
                {item[0]}
              </a>
            ))}
          </div>
        </div>
      ) : null}
    </main>
  );
}

async function api<T = Record<string, unknown>>(
  url: string,
  workspaceId: number,
  body: unknown,
  method = "POST",
): Promise<T> {
  const response = await fetch(url, {
    method,
    headers: {
      "content-type": "application/json",
      "x-workspace-id": String(workspaceId),
    },
    body: JSON.stringify(body),
  });
  const data = (await response.json()) as T & { error?: string; detail?: string };
  if (!response.ok) throw new Error(data.detail || data.error || "Request failed");
  return data;
}

function formatDuration(seconds: number) {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function maskPhone(value: string) {
  return value.length > 4 ? `•••• ${value.slice(-4)}` : "••••";
}

function csvCell(value: string) {
  const formulaSafeValue = /^[=+\-@\t\r]/.test(value) ? `'${value}` : value;
  return `"${formulaSafeValue.replaceAll('"', '""')}"`;
}

function hostFromUrl(value: string) {
  try {
    return new URL(value).hostname;
  } catch {
    return "invalid URL";
  }
}

function titleCase(value: string) {
  return value ? `${value[0].toUpperCase()}${value.slice(1)}` : value;
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Request failed";
}

function downloadText(filename: string, content: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
