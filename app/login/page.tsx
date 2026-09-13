import Link from "next/link";
import { redirect } from "next/navigation";
import { appHome, getSession } from "../../lib/server-api";
import AuthForm from "../shared/auth-form";

export const dynamic = "force-dynamic";

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ returnTo?: string }> }) {
  const session = await getSession();
  if (session) redirect(appHome(session));
  const { returnTo } = await searchParams;
  return <main className="auth-shell">
    <Link className="brand auth-brand" href="/"><span className="brand-mark">N</span><span>NEXORA</span></Link>
    <section className="auth-card">
      <small>SECURE SIGN IN</small>
      <h1>Welcome back</h1>
      <p>Use the account your workspace administrator invited.</p>
      <AuthForm mode="login" returnTo={returnTo}/>
      <p className="auth-switch">Need a new tenant workspace? <Link href="/register">Create one</Link></p>
    </section>
  </main>;
}
