import "server-only";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

export type AppUser = {
  id: number;
  email: string;
  name: string;
  isSuperadmin: boolean;
};

export type AppWorkspace = {
  id: number;
  name: string;
  slug: string;
  plan: string;
  status: string;
  role: "owner" | "admin" | "operator" | "member" | "viewer";
  region?: string;
  retentionDays?: number;
};

export type AppSession = {
  user: AppUser;
  workspaces: AppWorkspace[];
};

export class BackendError extends Error {
  readonly status: number;
  readonly details: unknown;

  constructor(status: number, message: string, details?: unknown) {
    super(message);
    this.name = "BackendError";
    this.status = status;
    this.details = details;
  }
}

type ApiOptions = RequestInit & { workspaceId?: number };

function backendOrigin(): string {
  const configured = process.env.FASTAPI_INTERNAL_URL || process.env.API_INTERNAL_URL || "http://127.0.0.1:8000";
  return configured.replace(/\/$/, "");
}

export async function serverApi<T>(path: string, options: ApiOptions = {}): Promise<T> {
  if (!path.startsWith("/api/")) throw new Error("Backend paths must start with /api/");
  const { workspaceId, ...requestOptions } = options;
  const cookieStore = await cookies();
  const headers = new Headers(options.headers);
  const cookieHeader = cookieStore.toString();
  if (cookieHeader) headers.set("cookie", cookieHeader);
  if (workspaceId !== undefined) headers.set("x-workspace-id", String(workspaceId));
  headers.set("accept", "application/json");

  const response = await fetch(`${backendOrigin()}${path}`, {
    ...requestOptions,
    headers,
    cache: "no-store",
    redirect: "manual",
  });
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof body === "object" && body && "detail" in body
      ? String((body as { detail: unknown }).detail)
      : typeof body === "object" && body && "error" in body
        ? String((body as { error: unknown }).error)
        : `Backend request failed (${response.status})`;
    throw new BackendError(response.status, message, body);
  }
  return body as T;
}

export async function optionalServerApi<T>(path: string, fallback: T, workspaceId?: number): Promise<T> {
  try {
    return await serverApi<T>(path, { workspaceId });
  } catch (error) {
    if (error instanceof BackendError && [403, 404, 501].includes(error.status)) return fallback;
    throw error;
  }
}

export async function getSession(): Promise<AppSession | null> {
  try {
    const raw = await serverApi<Record<string, unknown>>("/api/auth/me");
    const sourceUser = (raw.user || raw) as Record<string, unknown>;
    const memberships = Array.isArray(raw.workspaces)
      ? raw.workspaces
      : Array.isArray(raw.memberships)
        ? raw.memberships
        : raw.workspace
          ? [raw.workspace]
          : [];
    const user: AppUser = {
      id: Number(sourceUser.id || 0),
      email: String(sourceUser.email || ""),
      name: String(sourceUser.name || sourceUser.full_name || sourceUser.email || "User"),
      isSuperadmin: Boolean(sourceUser.is_superadmin ?? sourceUser.isSuperadmin),
    };
    if (!user.email) return null;
    return { user, workspaces: memberships.map(value => normalizeWorkspace(value as Record<string, unknown>)) };
  } catch (error) {
    if (error instanceof BackendError && error.status === 401) return null;
    throw error;
  }
}

export function appHome(session: AppSession): string {
  if (session.workspaces[0]) return "/dashboard";
  if (session.user.isSuperadmin) return "/superadmin/licenses";
  return "/register";
}

export async function requireSession(returnTo: string): Promise<AppSession> {
  const session = await getSession();
  if (!session) redirect(`/login?returnTo=${encodeURIComponent(safeReturnTo(returnTo))}`);
  return session;
}

export async function requireWorkspace(returnTo: string): Promise<{ session: AppSession; workspace: AppWorkspace }> {
  const session = await requireSession(returnTo);
  const workspace = session.workspaces[0];
  if (!workspace) redirect(appHome(session));
  return { session, workspace };
}

export async function licensedServerApi<T>(path: string, workspaceId: number): Promise<T> {
  try {
    return await serverApi<T>(path, { workspaceId });
  } catch (error) {
    if (error instanceof BackendError && error.status === 401) redirect("/login");
    if (error instanceof BackendError && error.status === 402) redirect("/settings?tab=license&reason=required");
    throw error;
  }
}

export async function getBrand(): Promise<string> {
  try {
    const payload = await serverApi<{ settings?: Array<{ key: string; value: unknown }> | Record<string, unknown> }>("/api/cms/settings");
    if (Array.isArray(payload.settings)) {
      const row = payload.settings.find(item => item.key === "brand.name");
      if (typeof row?.value === "string" && row.value.trim()) return row.value.trim();
    }
    if (payload.settings && !Array.isArray(payload.settings)) {
      const value = payload.settings["brand.name"];
      if (typeof value === "string" && value.trim()) return value.trim();
    }
  } catch {
    // Branding must not make the public or authenticated UI unavailable.
  }
  return "Nexora";
}

export function camelizeApi<T>(value: unknown): T {
  if (Array.isArray(value)) return value.map(item => camelizeApi(item)) as T;
  if (!value || typeof value !== "object") return value as T;
  const result: Record<string, unknown> = {};
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    if (["__proto__", "constructor", "prototype"].includes(key)) continue;
    const camelKey = key.replace(/_([a-z0-9])/g, (_, letter: string) => letter.toUpperCase());
    result[camelKey] = camelizeApi(child);
  }
  return result as T;
}

function normalizeWorkspace(value: Record<string, unknown>): AppWorkspace {
  const nested = value.workspace && typeof value.workspace === "object" ? value.workspace as Record<string, unknown> : value;
  return {
    id: Number(nested.id || value.workspace_id || 0),
    name: String(nested.name || "Workspace"),
    slug: String(nested.slug || "workspace"),
    plan: String(nested.plan || value.plan || "licensed"),
    status: String(nested.status || value.status || "pending-license"),
    role: String(value.role || nested.role || "viewer") as AppWorkspace["role"],
    region: typeof nested.region === "string" ? nested.region : undefined,
    retentionDays: Number(nested.retention_days || nested.retentionDays || 30),
  };
}

function safeReturnTo(value: string): string {
  if (!value.startsWith("/") || value.startsWith("//") || /[\\\u0000-\u001F\u007F]/.test(value)) return "/dashboard";
  try {
    const parsed = new URL(value, "https://app.local");
    const blockedAuthPath = ["/login", "/logout"].includes(parsed.pathname) || (parsed.pathname === "/register" && !parsed.searchParams.get("invite"));
    if (parsed.origin !== "https://app.local" || blockedAuthPath) return "/dashboard";
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return "/dashboard";
  }
}
