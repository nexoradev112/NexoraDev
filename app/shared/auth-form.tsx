"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

export default function AuthForm({ mode, returnTo, inviteToken }: { mode: "login" | "register"; returnTo?: string; inviteToken?: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({ name: "", workspaceName: "", email: "", password: "" });

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const payload = mode === "login"
        ? { email: form.email, password: form.password }
        : { email: form.email, password: form.password, name: form.name, ...(inviteToken ? { inviteToken } : { workspaceName: form.workspaceName }) };
      const response = await fetch(`/api/auth/${mode}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({})) as { error?: string; detail?: string };
      if (!response.ok) throw new Error(data.detail || data.error || (response.status === 429 ? "Too many attempts. Try again shortly." : "Unable to continue"));
      const destination = mode === "register" ? (inviteToken ? "/dashboard" : "/settings?tab=license") : safeDestination(returnTo);
      router.replace(destination);
      router.refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to continue");
    } finally {
      setBusy(false);
    }
  }

  return <form className="auth-form" onSubmit={submit}>
    {mode === "register" ? <>
      <label>Your name<input required autoComplete="name" maxLength={120} value={form.name} onChange={event => setForm({ ...form, name: event.target.value })}/></label>
      {!inviteToken ? <label>Company or workspace<input required autoComplete="organization" maxLength={120} value={form.workspaceName} onChange={event => setForm({ ...form, workspaceName: event.target.value })}/></label> : <p className="auth-hint">This account will be bound to the workspace and email in the signed invitation.</p>}
    </> : null}
    <label>Email address<input required type="email" autoComplete="email" maxLength={254} value={form.email} onChange={event => setForm({ ...form, email: event.target.value })}/></label>
    <label>Password<input required type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} minLength={12} maxLength={128} value={form.password} onChange={event => setForm({ ...form, password: event.target.value })}/></label>
    {mode === "register" ? <p className="auth-hint">Use at least 12 characters. Passwords are hashed by the Python API and are never stored or logged in plaintext.</p> : null}
    {error ? <p className="form-error" role="alert">{error}</p> : null}
    <button className="button" disabled={busy} type="submit">{busy ? "Please wait…" : mode === "login" ? "Sign in" : inviteToken ? "Create account and join" : "Create locked workspace"}</button>
  </form>;
}

function safeDestination(value?: string): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || /[\\\u0000-\u001F\u007F]/.test(value)) return "/dashboard";
  try {
    const parsed = new URL(value, window.location.origin);
    const blockedAuthPath = ["/login", "/logout"].includes(parsed.pathname) || (parsed.pathname === "/register" && !parsed.searchParams.get("invite"));
    if (parsed.origin !== window.location.origin || blockedAuthPath) return "/dashboard";
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return "/dashboard";
  }
}
