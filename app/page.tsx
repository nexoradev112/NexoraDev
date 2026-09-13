import Image from "next/image";
import {serverApi} from "../lib/server-api";

const capabilities = [
  ["01", "Design", "Build branching voice and chat agents with goals, provider policies, human approvals, and post-call actions."],
  ["02", "Connect", "Use OpenAI, Groq, Anthropic, Deepgram, ElevenLabs, LiveKit SIP, and signed HTTPS webhooks."],
  ["03", "Operate", "Test conversations, observe live calls, review recordings, and improve agents with QA and analytics data."],
];
const useCases = ["Customer support", "Sales qualification", "Appointment booking", "Collections", "Recruiting", "Internal operations"];

async function websiteSettings(){
  try{
    const payload=await serverApi<{settings?:Array<{key:string;value:unknown}>|Record<string,unknown>}>("/api/cms/settings");
    if(Array.isArray(payload.settings))return new Map(payload.settings.map(row=>[row.key,row.value]));
    return new Map(Object.entries(payload.settings||{}));
  }catch{return new Map<string,unknown>()}
}

export default async function Home() {
  const settings=await websiteSettings();
  const brand=String(settings.get("brand.name")||"Nexora"),heroTitle=String(settings.get("home.heroTitle")||"Build AI agents that reason, speak & act."),heroBody=String(settings.get("home.heroBody")||"One production platform to design, deploy, and govern autonomous voice and chat agents across your business."),primaryCta=String(settings.get("home.primaryCta")||"Build your first agent"),logoKey=String(settings.get("brand.logoKey")||"");
  return <main className="marketing-shell">
    <nav className="topbar"><a className="brand" href="#top">{logoKey?<Image className="cms-logo" src={`/api/media?key=${encodeURIComponent(logoKey)}`} alt={`${brand} logo`} width={160} height={48} unoptimized/>:<span className="brand-mark">{brand.slice(0,1).toUpperCase()}</span>}<span>{brand.toUpperCase()}</span></a><div className="navlinks"><a href="#platform">Platform</a><a href="#solutions">Solutions</a><a href="#governance">Governance</a></div><div className="nav-actions"><a className="text-link" href="/admin">Admin</a><a className="button small" href="/dashboard">Open app <span>↗</span></a></div></nav>
    <section className="hero" id="top"><div className="eyebrow"><span className="pulse"/> Agent infrastructure for real work</div><h1>{heroTitle}</h1><p>{heroBody}</p><div className="hero-actions"><a className="button" href="/dashboard">{primaryCta} <span>↗</span></a><a className="ghost-button" href="#platform"><span className="play">▶</span> See how it works</a></div><div className="trustline"><span>No credit card required</span><span>•</span><span>Provider independent</span><span>•</span><span>Human-in-the-loop</span></div>
      <div className="hero-product"><div className="product-top"><span className="dots">● ● ●</span><span>Returns Concierge / Production</span><span className="live">● LIVE</span></div><div className="product-body"><aside className="mini-sidebar"><b>Workflow</b><span className="active">⌘ Agent canvas</span><span>◫ Knowledge</span><span>◇ Evaluations</span><span>◌ Activity</span><div/><span>⚙ Settings</span></aside><div className="canvas"><div className="canvas-note">Inbound customer call</div><div className="flow-row"><div className="flow-card start"><i>01</i><b>Listen & identify</b><small>Voice · ElevenLabs</small></div><span className="connector">→</span><div className="flow-card"><i>02</i><b>Reason over policy</b><small>GPT · Knowledge base</small></div><span className="connector">→</span><div className="flow-card accent"><i>03</i><b>Resolve or approve</b><small>Tools · Guardrail</small></div></div><div className="runbar"><span><b>Run completed</b> · 18.4s</span><span>12 turns &nbsp; 3 tool calls &nbsp; $0.18</span></div></div></div></div>
    </section>
    <section className="logo-strip"><span>Compatible with</span><b>OpenAI</b><b>ANTHROPIC</b><b>Groq</b><b>Deepgram</b><b>ELEVENLABS</b><b>LiveKit</b></section>
    <section className="section" id="platform"><div className="section-intro"><p className="kicker">THE AGENT OPERATING SYSTEM</p><h2>From conversation<br/>to measurable outcomes.</h2><p>Most voice bots stop at answers. {brand} agents route conversations, escalate to people, run post-call QA, deliver approved webhooks, and preserve an audit trail.</p></div><div className="capability-list">{capabilities.map(([n,t,d])=><article key={n}><span>{n}</span><div><h3>{t}</h3><p>{d}</p></div><b>↗</b></article>)}</div></section>
    <section className="dark-section" id="solutions"><div><p className="kicker lime">BUILT FOR OUTCOMES</p><h2>One platform.<br/><em>Every conversation.</em></h2></div><div className="use-grid">{useCases.map((u,i)=><div key={u}><span>0{i+1}</span><h3>{u}</h3><p>Deploy a specialized agent with your policies, systems, and escalation rules.</p></div>)}</div></section>
    <section className="section governance" id="governance"><div className="governance-card"><p className="kicker">CONTROL WITHOUT COMPROMISE</p><h2>Autonomy you can trust.</h2><p>Version agent definitions. Gate sensitive actions. Redact personal data. Review call outcomes. Give each team exactly the access it needs.</p><div className="checks"><span>✓ Human approvals</span><span>✓ Audit trail</span><span>✓ Agent versioning</span><span>✓ Quota controls</span></div><a className="button dark" href="/studio">Explore the studio <span>↗</span></a></div><div className="audit-card"><div className="audit-head"><b>Example run</b><span>Passed</span></div>{[["Intent classified","return_request","42 ms"],["Policy retrieved","returns_v4.pdf","118 ms"],["Refund prepared","$84.50","302 ms"],["Human approval","Approved by Maya","1m 08s"]].map(r=><div className="audit-row" key={r[0]}><i>✓</i><div><b>{r[0]}</b><small>{r[1]}</small></div><span>{r[2]}</span></div>)}</div></section>
    <footer><div className="brand"><span className="brand-mark">{brand.slice(0,1).toUpperCase()}</span><span>{brand.toUpperCase()}</span></div><h2>Your next best operator<br/>is an AI agent.</h2><a className="button" href="/studio">Start building <span>↗</span></a><div className="footer-bottom"><span>© 2026 {brand}</span><span>Platform &nbsp; Security &nbsp; Documentation &nbsp; Contact</span></div></footer>
  </main>;
}
