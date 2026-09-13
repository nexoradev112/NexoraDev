import type { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const HOP_BY_HOP = new Set(["connection", "content-length", "host", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"]);

function apiOrigin(): string {
  return (process.env.FASTAPI_INTERNAL_URL || process.env.API_INTERNAL_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
}

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const { path } = await context.params;
  if (path[0]?.toLowerCase() === "internal") return Response.json({ error: "Not found" }, { status: 404 });
  const safePath = path.map(segment => encodeURIComponent(segment)).join("/");
  const incoming = new URL(request.url);
  const target = `${apiOrigin()}/api/${safePath}${incoming.search}`;
  const headers = new Headers();
  request.headers.forEach((value, key) => {
    const lower = key.toLowerCase();
    if (HOP_BY_HOP.has(lower) || lower.startsWith("x-internal-") || lower === "x-call-worker-token" || lower.startsWith("x-forwarded-")) return;
    headers.append(key, value);
  });
  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  const response = await fetch(target, {
    method: request.method,
    headers,
    body: hasBody ? await request.arrayBuffer() : undefined,
    cache: "no-store",
    redirect: "manual",
  });
  const responseHeaders = new Headers();
  response.headers.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key.toLowerCase()) && key.toLowerCase() !== "set-cookie") responseHeaders.append(key, value);
  });
  const headersWithCookies = response.headers as Headers & { getSetCookie?: () => string[] };
  const combinedCookie = response.headers.get("set-cookie");
  const setCookies = headersWithCookies.getSetCookie?.() || (combinedCookie ? splitSetCookie(combinedCookie) : []);
  for (const cookie of setCookies) responseHeaders.append("set-cookie", cookie);
  responseHeaders.set("cache-control", "no-store");
  return new Response(response.body, { status: response.status, headers: responseHeaders });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;

function splitSetCookie(value: string): string[] {
  return value.split(/,(?=\s*[^;,=\s]+=)/g).map(cookie => cookie.trim()).filter(Boolean);
}
