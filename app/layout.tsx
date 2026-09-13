import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Nexora — Build AI Agents That Act",
  description: "Design, deploy, and govern production AI voice and chat agents with tools, workflows, knowledge, and human approvals.",
  metadataBase: new URL(process.env.APP_PUBLIC_URL || "http://localhost:3000"),
  openGraph: {
    title: "Nexora — Build AI Agents That Act",
    description: "Design, deploy, and govern AI voice and chat agents that complete real work.",
    images: [{ url: "/og.png", width: 1200, height: 630, alt: "Nexora AI agent platform" }],
  },
  twitter: {
    card: "summary_large_image",
    title: "Nexora — Build AI Agents That Act",
    description: "AI agents that reason, speak, and act.",
    images: ["/og.png"],
  },
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
