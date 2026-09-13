import { getSession, requireSession, type AppUser } from "../lib/server-api";

export type ChatGPTUser = {
  displayName: string;
  email: string;
  fullName: string | null;
  id: number;
  isSuperadmin: boolean;
};

export async function getChatGPTUser(): Promise<ChatGPTUser | null> {
  const session = await getSession();
  return session ? toLegacyUser(session.user) : null;
}

export async function requireChatGPTUser(
  returnTo: string,
): Promise<ChatGPTUser> {
  return toLegacyUser((await requireSession(returnTo)).user);
}

export function chatGPTSignInPath(returnTo: string): string {
  const safeReturnTo = safeRelativeReturnPath(returnTo);
  return `/login?returnTo=${encodeURIComponent(safeReturnTo)}`;
}

export function chatGPTSignOutPath(returnTo = "/"): string {
  void returnTo;
  return "/logout";
}

function safeRelativeReturnPath(value: string): string {
  if (!value.startsWith("/") || value.startsWith("//")) return "/";

  let url: URL;
  try {
    url = new URL(value, "https://app.local");
  } catch {
    return "/";
  }
  if (url.origin !== "https://app.local") return "/";
  if (isReservedAuthPath(url.pathname)) return "/";

  return `${url.pathname}${url.search}${url.hash}`;
}

function isReservedAuthPath(pathname: string): boolean {
  return pathname === "/login" || pathname === "/register" || pathname === "/logout";
}

function toLegacyUser(user: AppUser): ChatGPTUser {
  return { displayName: user.name, email: user.email, fullName: user.name, id: user.id, isSuperadmin: user.isSuperadmin };
}
