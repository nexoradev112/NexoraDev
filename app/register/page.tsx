import Link from "next/link";
import { redirect } from "next/navigation";
import { getSession } from "../../lib/server-api";
import AuthForm from "../shared/auth-form";
import InviteAcceptance from "./invite-acceptance";

export const dynamic = "force-dynamic";

export default async function RegisterPage({ searchParams }: { searchParams: Promise<{ invite?: string }> }) {
  const { invite } = await searchParams;
  const session = await getSession();
  if (session && !invite && session.workspaces.length) redirect("/settings?tab=license");
  if (session && !invite && session.user.isSuperadmin) redirect("/superadmin/licenses");
  if (session && !invite) {
    return <main className="auth-shell">
      <Link className="brand auth-brand" href="/"><span className="brand-mark">N</span><span>NEXORA</span></Link>
      <section className="auth-card">
        <small>NO WORKSPACE</small>
        <h1>This account has no tenant</h1>
        <p>Sign out, then create a workspace with a different email, or accept an invitation sent to {session.user.email}.</p>
        <p className="auth-switch"><Link href="/logout">Sign out</Link></p>
      </section>
    </main>;
  }
  if (session && invite) {
    return <main className="auth-shell">
      <Link className="brand auth-brand" href="/"><span className="brand-mark">N</span><span>NEXORA</span></Link>
      <section className="auth-card auth-card-wide">
        <small>WORKSPACE INVITATION</small>
        <h1>Join the workspace</h1>
        <p>Confirm the invitation with your signed-in account.</p>
        <InviteAcceptance inviteToken={invite} userEmail={session.user.email}/>
      </section>
    </main>;
  }
  const loginReturnTo = invite ? `/register?invite=${encodeURIComponent(invite)}` : "";
  return <main className="auth-shell">
    <Link className="brand auth-brand" href="/"><span className="brand-mark">N</span><span>NEXORA</span></Link>
    <section className="auth-card auth-card-wide">
      <small>TENANT SETUP</small>
      <h1>{invite ? "Join the workspace" : "Create your workspace"}</h1>
      <p>{invite ? "Create your account to accept the email-bound workspace invitation." : "The workspace starts locked. Paste the license issued by the platform operator to activate it."}</p>
      <AuthForm mode="register" inviteToken={invite}/>
      <p className="auth-switch">Already have an account? <Link href={loginReturnTo ? `/login?returnTo=${encodeURIComponent(loginReturnTo)}` : "/login"}>Sign in</Link></p>
    </section>
  </main>;
}
