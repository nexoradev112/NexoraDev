"use client";

export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return <main className="state-shell"><div className="state-card"><small>REQUEST FAILED</small><h1>We couldn’t load this page</h1><p>{safeMessage(error.message)}</p><div className="hero-actions"><button className="button" type="button" onClick={reset}>Try again</button><a className="ghost-button" href="/settings">Open tenant settings</a></div></div></main>;
}

function safeMessage(message: string): string {
  return /license/i.test(message) ? "Your workspace license needs attention. Open tenant settings to review it." : "The service is temporarily unavailable. Please try again.";
}
