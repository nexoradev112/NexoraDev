"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import TestSession from "../studio/test-session";

type Workspace = { id: number; name: string; slug: string; plan: string; status: string; role: string };
type Call = { id: number; agentId: number; direction: string; status: string; durationSeconds: number; summary: string | null; sentiment: string | null; createdAt: string };
type Agent = { id: number; name: string; channel?: string; locale?: string; status: string; updatedAt?: string };

export default function Dashboard({ userName, brand, workspace, metrics, recentCalls, recentAgents }: { userName: string; brand: string; workspace: Workspace; metrics: { agents: number; calls: number; campaigns: number; knowledge: number }; recentCalls: Call[]; recentAgents: Agent[] }) {
  const [callRows, setCallRows] = useState(recentCalls);
  const [toNumber, setToNumber] = useState("");
  const [agentId, setAgentId] = useState(String(recentAgents.find(agent => agent.status === "published")?.id || ""));
  const [callStatus, setCallStatus] = useState("");
  const [testAgent, setTestAgent] = useState<Agent | null>(null);
  const published = useMemo(() => recentAgents.filter(agent => agent.status === "published"), [recentAgents]);

  async function startCall() {
    setCallStatus("Starting…");
    try {
      const response = await fetch("/api/calls", { method: "POST", headers: { "content-type": "application/json", "x-workspace-id": String(workspace.id) }, body: JSON.stringify({ agentId: Number(agentId), toNumber }) });
      const data = await response.json() as { call?: { id: number; status: string }; error?: string; detail?: string };
      if (!response.ok || !data.call) throw new Error(data.detail || data.error || "Call could not be started");
      setCallStatus(`Call #${data.call.id} is ${data.call.status}`);
      setCallRows(rows => [{ id: data.call!.id, agentId: Number(agentId), direction: "outbound", status: data.call!.status, durationSeconds: 0, summary: null, sentiment: null, createdAt: new Date().toISOString() }, ...rows]);
    } catch (error) {
      setCallStatus(error instanceof Error ? error.message : "Call could not be started");
    }
  }

  return <main className="ops-shell">
    <aside className="ops-side">
      <Link className="brand" href="/"><span className="brand-mark">{brand.slice(0,1).toUpperCase()}</span><span>{brand.toUpperCase()}</span></Link>
      <div className="workspace-chip"><small>WORKSPACE</small><b>{workspace.name}</b><span>{workspace.plan} · {workspace.role}</span></div>
      <nav><a className="on" href="#overview">⌂ <span>Overview</span></a><Link href="/agents">✦ <span>Agents</span></Link><Link href="/operations#observe">◉ <span>Observe</span></Link><Link href="/operations#campaigns">◫ <span>Campaigns</span></Link><a href="#knowledge">▤ <span>Knowledge</span></a><Link href="/operations#connections">◇ <span>Connections</span></Link><Link href="/settings">⚿ <span>Tenant settings</span></Link></nav>
      <div className="ops-user"><span>{userName.slice(0,1).toUpperCase()}</span><div><b>{userName}</b><small>{workspace.status}</small><Link href="/logout">Sign out</Link></div></div>
    </aside>
    <section className="ops-main">
      <header><div><small>VOICE + CHAT OPERATIONS</small><h1>Good to see you.</h1><p>Build agents, launch calls, and review every outcome from one tenant-isolated workspace.</p></div><div><Link className="ghost-button" href="/settings">Settings</Link><Link className="button small" href="/studio?new=1">＋ New agent</Link></div></header>
      <div className="ops-content" id="overview">
        <section className="ops-metrics">
          <article><span>✦</span><small>Agents</small><b>{metrics.agents}</b><em>{published.length} published</em></article>
          <article><span>◉</span><small>Total calls</small><b>{metrics.calls}</b><em>Inbound + outbound</em></article>
          <article><span>◫</span><small>Campaigns</small><b>{metrics.campaigns}</b><em>Dispatcher · Phase 2</em></article>
          <article><span>▤</span><small>Knowledge</small><b>{metrics.knowledge}</b><em>Tenant-scoped sources</em></article>
        </section>
        <section className="ops-grid" id="calls">
          <article className="ops-panel wide"><div className="panel-head"><div><small>LIVE OPERATIONS</small><h2>Recent calls</h2></div><span className="health">● Signed worker callbacks</span></div>
            <div className="call-table"><div className="call-row head"><span>Call</span><span>Direction</span><span>Status</span><span>Duration</span><span>Outcome</span></div>{callRows.length ? callRows.map(call => <div className="call-row" key={call.id}><b>#{call.id}</b><span>{call.direction}</span><em className={`call-${call.status}`}>{call.status}</em><span>{formatDuration(call.durationSeconds)}</span><span>{call.summary || call.sentiment || "Waiting for result"}</span></div>) : <p className="empty">No calls yet. Publish an agent and place a test call.</p>}</div>
          </article>
          <article className="ops-panel dialer"><small>OUTBOUND CALL</small><h2>Call a customer</h2><p>Uses the configured LiveKit SIP trunk. Current voice-contact consent is required; recording-enabled agents also require separate recording consent. The destination is not returned by the API or written to application logs.</p><label>Published agent<select value={agentId} onChange={event => setAgentId(event.target.value)}><option value="">Select an agent</option>{published.map(agent => <option value={agent.id} key={agent.id}>{agent.name}</option>)}</select></label><label>Destination<input value={toNumber} onChange={event => setToNumber(event.target.value)} placeholder="+14155550123" inputMode="tel"/></label><button className="button" type="button" disabled={!agentId || !/^\+[1-9]\d{7,14}$/.test(toNumber)} onClick={() => void startCall()}>Start call</button>{callStatus ? <p className="form-status" role="status">{callStatus}</p> : null}</article>
        </section>
        <section className="ops-grid" id="campaigns">
          <article className="ops-panel"><div className="panel-head"><div><small>AGENTS</small><h2>Deployment roster</h2></div><Link className="mini-action" href="/studio?generate=1&new=1">＋ Generate agent</Link></div>{recentAgents.length ? recentAgents.map(agent => <div className="agent-line agent-actions" key={agent.id}><span>✦</span><div><b>{agent.name}</b><small>{agent.channel && agent.locale ? `${agent.channel} · ${agent.locale}` : "Open the agent to view its channel and language"}</small></div><div><em>{agent.status}</em><button type="button" onClick={() => setTestAgent(agent)}>Talk</button><button type="button" disabled={agent.status !== "published"} onClick={() => { setAgentId(String(agent.id)); window.location.hash = "calls"; }}>Call</button><Link href={`/studio?agentId=${agent.id}`}>Edit</Link></div></div>) : <p className="empty">Create your first multilingual agent.</p>}<Link className="mini-action" href="/agents">View all agents</Link></article>
          <article className="ops-panel capability-panel"><small>PRODUCTION CAPABILITIES</small><h2>Voice intelligence loop</h2><div className="capability-list"><p><span>1</span><b>Design</b><small>Flow, tools, approvals, handoff</small></p><p><span>2</span><b>Connect</b><small>LiveKit SIP, explicit provider routing</small></p><p><span>3</span><b>Operate</b><small>Tests, calls, live occupancy</small></p><p><span>4</span><b>Improve</b><small>QA, summaries, sentiment, dispositions</small></p></div><Link className="mini-action" href="/operations">Open operations console</Link></article>
        </section>
        <section className="ops-security" id="security"><div><small>SAAS CONTROL PLANE</small><h2>Isolation is enforced server-side.</h2><p>Workspace membership is checked for every agent, call, campaign, knowledge source, provider credential, and LiveKit room. Browser-supplied workspace IDs are never trusted by themselves.</p></div><div className="security-pills"><span>Per-tenant API keys</span><span>AES-GCM credential vault</span><span>Verified LiveKit webhooks</span><span>Audit trail</span><span>Rate limits</span><span>Role-based access</span></div></section>
      </div>{testAgent ? <TestSession agentId={testAgent.id} workspaceId={workspace.id} agentName={testAgent.name} brand={brand} onClose={() => setTestAgent(null)}/> : null}
    </section>
  </main>;
}

function formatDuration(seconds: number) {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
