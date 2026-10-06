import Link from "next/link";
import { requireSession, type AppUser } from "../../lib/server-api";
import TenantSettings from "./tenant-settings";

export const dynamic = "force-dynamic";

export default async function SettingsPage({ searchParams }: { searchParams: Promise<{ tab?: string; reason?: string }> }) {
  const session = await requireSession("/settings");
  const workspace = session.workspaces[0];
  const params = await searchParams;
  if (!workspace) return <WorkspaceRequired user={session.user}/>;
  return <TenantSettings
    user={session.user}
    workspace={workspace}
    initialTab={params.tab}
    licenseRequired={params.reason === "required"}
  />;
}

function WorkspaceRequired({ user }: { user: AppUser }) {
  return <main className="settings-shell">
    <aside className="settings-side">
      <Link className="brand" href="/"><span className="brand-mark">N</span><span>NEXORA</span></Link>
      <p>ACCOUNT</p>
      <nav><button className="on" type="button">⚙<span>Settings</span></button></nav>
      <div className="settings-account"><b>{user.name}</b><small>{user.email}</small>{user.isSuperadmin ? <Link href="/admin">Site administration</Link> : null}<Link href="/logout">Sign out</Link></div>
    </aside>
    <section className="settings-main">
      <header><div><small>ACCOUNT</small><h1>Settings</h1></div></header>
      <div className="settings-content">
        <section className="settings-panel">
          <small>WORKSPACE REQUIRED</small>
          <h2>No workspace on this account</h2>
          <p>License, provider keys, and members belong to a workspace. This account is not a member of one, so those settings cannot load.</p>
          <div className="hero-actions">
            {user.isSuperadmin
              ? <><Link className="button" href="/superadmin/licenses">License console</Link><Link className="ghost-button" href="/admin">Site administration</Link></>
              : <Link className="button" href="/register">Create a workspace</Link>}
          </div>
        </section>
      </div>
    </section>
  </main>;
}
