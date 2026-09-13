import { getBrand, licensedServerApi, requireWorkspace } from "../../lib/server-api";
import Dashboard from "./dashboard";

export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  const { session, workspace } = await requireWorkspace("/dashboard");
  const [payload, brand] = await Promise.all([
    licensedServerApi<DashboardPayload>("/api/dashboard", workspace.id),
    getBrand(),
  ]);
  const recentCalls = (payload.recent_calls || payload.recentCalls || []).map(call => ({
    id: Number(call.id), agentId: Number(call.agent_id ?? call.agentId), direction: call.direction, status: call.status,
    durationSeconds: Number(call.duration_seconds ?? call.durationSeconds ?? 0), summary: call.summary || null,
    sentiment: call.sentiment || null, createdAt: String(call.created_at ?? call.createdAt ?? ""),
  }));
  const recentAgents = (payload.recent_agents || payload.recentAgents || payload.agents || []).map(agent => ({
    id: Number(agent.id), name: agent.name, channel: agent.channel || undefined, locale: agent.locale || undefined, status: agent.status,
    updatedAt: String(agent.updated_at ?? agent.updatedAt ?? ""),
  }));
  const metrics = payload.metrics || payload.stats || {};
  return <Dashboard userName={session.user.name} brand={brand} workspace={workspace} metrics={{ agents: Number(metrics.agents ?? metrics.totalAgents ?? recentAgents.length), calls: Number(metrics.calls ?? metrics.totalCalls ?? recentCalls.length), campaigns: Number(metrics.campaigns ?? metrics.totalCampaigns ?? 0), knowledge: Number(metrics.knowledge ?? metrics.totalKnowledge ?? 0) }} recentCalls={recentCalls} recentAgents={recentAgents}/>;
}

type DashboardPayload = {
  metrics?: DashboardMetrics; stats?: DashboardMetrics;
  recent_calls?: RawCall[]; recentCalls?: RawCall[];
  recent_agents?: RawAgent[]; recentAgents?: RawAgent[];
  agents?: RawAgent[];
};
type DashboardMetrics={agents?:number;calls?:number;campaigns?:number;knowledge?:number;totalAgents?:number;totalCalls?:number;totalCampaigns?:number;totalKnowledge?:number;liveCalls?:number;voiceSeconds?:number;tokensUsed?:number;seatsUsed?:number;seatsLimit?:number};
type RawCall = { id: number; agent_id?: number; agentId?: number; direction: string; status: string; duration_seconds?: number; durationSeconds?: number; summary?: string | null; sentiment?: string | null; created_at?: string; createdAt?: string };
type RawAgent = { id: number; name: string; channel?: string; locale?: string; status: string; updated_at?: string; updatedAt?: string };
