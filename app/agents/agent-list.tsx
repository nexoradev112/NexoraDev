"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import TestSession from "../studio/test-session";

type Agent = { id: number; name: string; objective: string; channel: string; locale: string; status: string; updatedAt: string };
type Workspace = { id: number; name: string; plan: string; role: string };

export default function AgentList({ workspace, brand, initialAgents, canBuild, canCall, canArchive }: { workspace: Workspace; brand: string; initialAgents: Agent[]; canBuild: boolean; canCall: boolean; canArchive: boolean }) {
  const [rows, setRows] = useState(initialAgents);
  const [tab, setTab] = useState<"active" | "archived">("active");
  const [search, setSearch] = useState("");
  const [testAgent, setTestAgent] = useState<Agent | null>(null);
  const [callAgent, setCallAgent] = useState<Agent | null>(null);
  const [number, setNumber] = useState("");
  const [notice, setNotice] = useState("");
  const filtered = useMemo(() => rows.filter(row => (tab === "archived" ? row.status === "archived" : row.status !== "archived") && `${row.name} ${row.objective} ${row.locale}`.toLowerCase().includes(search.toLowerCase())), [rows, tab, search]);

  async function archive(agent: Agent) {
    if (!confirm(`Archive ${agent.name}?`)) return;
    const response = await fetch(`/api/agents?id=${agent.id}`, { method: "DELETE", headers: { "x-workspace-id": String(workspace.id) } });
    const result = await response.json() as { error?: string };
    if (!response.ok) { setNotice(result.error || "Agent could not be archived"); return; }
    setRows(current => current.map(row => row.id === agent.id ? { ...row, status: "archived" } : row));
    setNotice(`${agent.name} archived`);
  }

  async function call() {
    if (!callAgent) return;
    setNotice("Starting phone call…");
    const response = await fetch("/api/calls", { method: "POST", headers: { "content-type": "application/json", "x-workspace-id": String(workspace.id) }, body: JSON.stringify({ agentId: callAgent.id, toNumber: number }) });
    const result = await response.json() as { call?: { id: number; status: string }; error?: string };
    if (!response.ok || !result.call) { setNotice(result.error || "Call could not be started"); return; }
    setNotice(`Call #${result.call.id} is ${result.call.status}`);
    setCallAgent(null); setNumber("");
  }

  return <main className="ops-shell">
    <aside className="ops-side"><Link className="brand" href="/dashboard"><span className="brand-mark">{brand.slice(0, 1).toUpperCase()}</span><span>{brand.toUpperCase()}</span></Link><div className="workspace-chip"><small>WORKSPACE</small><b>{workspace.name}</b><span>{workspace.plan} · {workspace.role}</span></div><nav><Link href="/dashboard">⌂ <span>Overview</span></Link><Link className="on" href="/agents">✦ <span>Agents</span></Link><Link href="/analytics">◉ <span>Analytics</span></Link><Link href="/reports">▤ <span>Reports</span></Link><Link href="/recordings">♫ <span>Audio</span></Link><Link href="/telephony">☎ <span>Telephony</span></Link></nav></aside>
    <section className="ops-main">
      <header><div><small>BUILD · AGENTS</small><h1>Agent roster</h1><p>Generate, design, test, call, and manage every workspace agent.</p></div>{canBuild ? <div><Link className="ghost-button" href="/studio?new=1">Blank canvas</Link><Link className="button small" href="/studio?new=1&generate=1">✦ Generate agent</Link></div> : null}</header>
      <div className="ops-content agent-roster">
        {notice ? <p className="ops-notice" role="status">{notice}<button type="button" onClick={() => setNotice("")}>×</button></p> : null}
        {canBuild ? <TemplateStrip /> : null}
        <section className="ops-panel"><div className="panel-head"><div className="range-tabs"><button type="button" className={tab === "active" ? "active" : ""} onClick={() => setTab("active")}>Active ({rows.filter(row => row.status !== "archived").length})</button><button type="button" className={tab === "archived" ? "active" : ""} onClick={() => setTab("archived")}>Archived ({rows.filter(row => row.status === "archived").length})</button></div><input className="roster-search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search agents…" aria-label="Search agents" /></div>
          <div className="roster-table"><div className="head"><span>Agent</span><span>Channel</span><span>Language</span><span>Status</span><span>Updated</span><span>Actions</span></div>{filtered.map(agent => <div key={agent.id}><span><b>{agent.name}</b><small>{agent.objective || "No objective supplied"}</small></span><span>{agent.channel}</span><span>{agent.locale}</span><span><em className={`call-${agent.status}`}>{agent.status}</em></span><span>{new Date(agent.updatedAt).toLocaleDateString()}</span><span className="roster-actions">{agent.status !== "archived" ? <>{canBuild ? <button type="button" onClick={() => setTestAgent(agent)}>Talk</button> : null}{canCall ? <button type="button" disabled={agent.status !== "published"} onClick={() => setCallAgent(agent)}>Call</button> : null}<Link href={`/studio?agentId=${agent.id}`}>{canBuild ? "Edit" : "View"}</Link>{canArchive ? <button type="button" className="danger-text" onClick={() => void archive(agent)}>Archive</button> : null}</> : <a href={`/api/agents/definition?id=${agent.id}&workspace=${workspace.id}`}>Export</a>}</span></div>)}{!filtered.length ? <p className="empty">No {tab} agents match this search.</p> : null}</div>
        </section>
      </div>
      {testAgent ? <TestSession agentId={testAgent.id} workspaceId={workspace.id} agentName={testAgent.name} brand={brand} onClose={() => setTestAgent(null)} /> : null}
      {callAgent ? <div className="modal-backdrop"><div className="product-modal" role="dialog" aria-label="Place phone call"><div className="panel-head"><div><small>PHONE TEST</small><h2>Call with {callAgent.name}</h2></div><button type="button" onClick={() => setCallAgent(null)}>×</button></div><label>Destination<input inputMode="tel" placeholder="+14155550123" value={number} onChange={event => setNumber(event.target.value)} /></label><button className="button" type="button" disabled={!/^\+[1-9]\d{7,14}$/.test(number)} onClick={() => void call()}>Start phone call</button></div></div> : null}
    </section>
  </main>;
}

function TemplateStrip() {
  return <section className="template-strip" aria-label="Agent templates"><div><small>START FROM A TEMPLATE</small><b>Production-ready workflow starters</b></div><Link href="/studio?new=1&template=customer-support"><b>Customer support</b><span>Resolution, tools, handoff, QA</span></Link><Link href="/studio?new=1&template=lead-qualification"><b>Lead qualification</b><span>Fit branches and disposition</span></Link><Link href="/studio?new=1&template=appointment-booking"><b>Appointment booking</b><span>Schedule, exceptions, confirmation</span></Link></section>;
}
