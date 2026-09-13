"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

export default function InviteAcceptance({
  inviteToken,
  userEmail,
}: {
  inviteToken: string;
  userEmail: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [accepted, setAccepted] = useState(false);

  async function accept() {
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/invites/accept", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ token: inviteToken }),
      });
      const data = (await response.json().catch(() => ({}))) as {
        accepted?: boolean;
        detail?: string;
        error?: string;
      };
      if (!response.ok || !data.accepted) {
        throw new Error(data.detail || data.error || "Invitation could not be accepted");
      }
      setAccepted(true);
      router.refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Invitation could not be accepted");
    } finally {
      setBusy(false);
    }
  }

  if (accepted) {
    return (
      <div className="auth-form">
        <p className="auth-hint" role="status">
          Invitation accepted. Your workspace membership is active.
        </p>
        <button className="button" type="button" onClick={() => router.replace("/dashboard")}>
          Open dashboard
        </button>
      </div>
    );
  }

  return (
    <div className="auth-form">
      <p className="auth-hint">
        You are signed in as <b>{userEmail}</b>. The invitation is email-bound and the server will
        verify the licensed seat before adding this account.
      </p>
      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
      <button className="button" type="button" disabled={busy} onClick={() => void accept()}>
        {busy ? "Checking invitation…" : "Accept workspace invitation"}
      </button>
      <p className="auth-switch">
        Invitation for another email? <Link href="/logout">Sign out</Link>
      </p>
    </div>
  );
}
