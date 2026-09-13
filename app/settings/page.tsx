import { requireWorkspace } from "../../lib/server-api";
import TenantSettings from "./tenant-settings";

export const dynamic = "force-dynamic";

export default async function SettingsPage({ searchParams }: { searchParams: Promise<{ tab?: string; reason?: string }> }) {
  const { session, workspace } = await requireWorkspace("/settings");
  const params = await searchParams;
  return <TenantSettings
    user={session.user}
    workspace={workspace}
    initialTab={params.tab}
    licenseRequired={params.reason === "required"}
  />;
}
