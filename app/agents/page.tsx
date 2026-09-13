import { getBrand, licensedServerApi, requireWorkspace } from "../../lib/server-api";
import AgentList from "./agent-list";

export const dynamic = "force-dynamic";

export default async function AgentsPage() {
  const { workspace } = await requireWorkspace("/agents");
  const [payload, brand] = await Promise.all([
    licensedServerApi<{ agents?: RawAgent[] }>("/api/agents", workspace.id),
    getBrand(),
  ]);
  const rows = (payload.agents || []).map(row => ({ id: Number(row.id), name: row.name, objective: row.objective || "", channel: row.channel, locale: row.locale, status: row.status, updatedAt: String(row.updated_at ?? row.updatedAt ?? "") }));
  return <AgentList workspace={{ id: workspace.id, name: workspace.name, plan: workspace.plan, role: workspace.role }} brand={brand} initialAgents={rows} canBuild={allows(workspace.role,"member")} canCall={allows(workspace.role,"operator")} canArchive={allows(workspace.role,"admin")}/>;
}

type RawAgent={id:number;name:string;objective?:string;channel:string;locale:string;status:string;updated_at?:string;updatedAt?:string};
const levels={viewer:0,member:1,operator:2,admin:3,owner:4};
function allows(role:string,minimum:keyof typeof levels){return (levels[role as keyof typeof levels]??-1)>=levels[minimum]}
