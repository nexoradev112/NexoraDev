"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function LogoutPage() {
  const router = useRouter();
  useEffect(() => {
    void fetch("/api/auth/logout", { method: "POST" }).finally(() => {
      router.replace("/login");
      router.refresh();
    });
  }, [router]);
  return <main className="auth-shell"><section className="auth-card"><h1>Signing out…</h1><p>Your browser session is being closed.</p></section></main>;
}
