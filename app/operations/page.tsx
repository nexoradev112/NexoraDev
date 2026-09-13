import { camelizeApi, getBrand, licensedServerApi, optionalServerApi, requireWorkspace } from "../../lib/server-api";
import OperationsConsole from "./operations-console";

export const dynamic = "force-dynamic";

export default async function OperationsPage() {
  const { session, workspace } = await requireWorkspace("/operations");
  const canOperate = ["operator", "admin", "owner"].includes(workspace.role);
  const canAdmin = ["admin", "owner"].includes(workspace.role);
  const agentPayload = await licensedServerApi<{ agents?: unknown[] }>("/api/agents", workspace.id);
  const [callPayload, campaignPayload, integrationPayload, phonePayload, providerPayload, usagePayload, auditPayload, memberPayload, compliancePayload, reservationPayload, billingPayload, brand] = await Promise.all([
    optionalServerApi<{ calls?: unknown[] }>("/api/calls", {}, workspace.id),
    optionalServerApi<{ campaigns?: unknown[] }>("/api/campaigns", {}, workspace.id),
    canAdmin ? optionalServerApi<{ integrations?: unknown[] }>("/api/integrations", {}, workspace.id) : Promise.resolve({ integrations: [] as unknown[] }),
    canAdmin ? optionalServerApi<{ phoneNumbers?: unknown[]; phone_numbers?: unknown[] }>("/api/phone-numbers", {}, workspace.id) : Promise.resolve({ phoneNumbers: [] as unknown[], phone_numbers: [] as unknown[] }),
    canAdmin ? optionalServerApi<{ providers?: unknown[]; connections?: unknown[] }>("/api/providers", {}, workspace.id) : Promise.resolve({ providers: [] as unknown[], connections: [] as unknown[] }),
    optionalServerApi<{ usage?: unknown[] }>("/api/usage", {}, workspace.id),
    canAdmin ? optionalServerApi<{ audit?: unknown[]; events?: unknown[] }>("/api/audit", {}, workspace.id) : Promise.resolve({ audit: [] as unknown[], events: [] as unknown[] }),
    canAdmin ? optionalServerApi<{ members?: unknown[] }>("/api/members", {}, workspace.id) : Promise.resolve({ members: [] as unknown[] }),
    canAdmin ? optionalServerApi<{ suppressions?: unknown[]; consents?: unknown[] }>("/api/compliance", {}, workspace.id) : Promise.resolve({ suppressions: [] as unknown[], consents: [] as unknown[] }),
    canAdmin ? optionalServerApi<{ reservations?: unknown[] }>("/api/usage/reservations", {}, workspace.id) : Promise.resolve({ reservations: [] as unknown[] }),
    canAdmin ? optionalServerApi<{ subscription?: unknown }>("/api/billing", {}, workspace.id) : Promise.resolve({ subscription: undefined as unknown }),
    getBrand(),
  ]);

  type ConsoleProps = Parameters<typeof OperationsConsole>[0];
  const props: ConsoleProps = {
    userName: session.user.name,
    brand,
    workspace: { id: workspace.id, name: workspace.name, slug: workspace.slug, plan: workspace.plan, status: workspace.status, role: workspace.role, region: workspace.region || "global", retentionDays: workspace.retentionDays || 30 },
    canOperate,
    canAdmin,
    agents: camelizeApi<ConsoleProps["agents"]>(agentPayload.agents || []),
    calls: camelizeApi<ConsoleProps["calls"]>(callPayload.calls || []),
    campaigns: camelizeApi<ConsoleProps["campaigns"]>(campaignPayload.campaigns || []),
    integrations: camelizeApi<ConsoleProps["integrations"]>(integrationPayload.integrations || []),
    phoneNumbers: camelizeApi<ConsoleProps["phoneNumbers"]>(phonePayload.phoneNumbers || phonePayload.phone_numbers || []),
    providers: camelizeApi<ConsoleProps["providers"]>(providerPayload.providers || providerPayload.connections || []),
    usage: camelizeApi<ConsoleProps["usage"]>(usagePayload.usage || []),
    audit: camelizeApi<ConsoleProps["audit"]>(auditPayload.audit || auditPayload.events || []),
    members: camelizeApi<ConsoleProps["members"]>(memberPayload.members || []),
    suppressions: camelizeApi<ConsoleProps["suppressions"]>(compliancePayload.suppressions || []),
    consents: camelizeApi<ConsoleProps["consents"]>(compliancePayload.consents || []),
    reservations: camelizeApi<ConsoleProps["reservations"]>(reservationPayload.reservations || []),
    subscription: billingPayload.subscription ? camelizeApi<ConsoleProps["subscription"]>(billingPayload.subscription) : null,
  };
  return <OperationsConsole {...props}/>;
}
