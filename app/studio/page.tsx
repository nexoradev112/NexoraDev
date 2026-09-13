import { getBrand, licensedServerApi, optionalServerApi, requireWorkspace } from "../../lib/server-api";
import Studio from "./studio";

export const dynamic = "force-dynamic";

export default async function StudioPage({ searchParams }: { searchParams: Promise<{ agentId?: string; generate?: string; new?: string; template?: string }> }) {
  const { workspace } = await requireWorkspace("/studio");
  const params = await searchParams;
  const requestedId = typeof params.agentId === "string" && /^[1-9]\d{0,9}$/.test(params.agentId) ? Number(params.agentId) : null;
  const agentQuery = params.new === "1"
    ? Promise.resolve(null)
    : requestedId
      ? licensedServerApi<{ agent?: RawAgent }>(`/api/agents/${requestedId}`, workspace.id).then(payload => payload.agent || null)
      : licensedServerApi<{ agents?: RawAgent[] }>("/api/agents?limit=1", workspace.id).then(payload => payload.agents?.[0] || null);
  const [brand, rawAgent, integrationPayload, recordingPayload] = await Promise.all([
    getBrand(), agentQuery,
    ["admin", "owner"].includes(workspace.role) ? optionalServerApi<{ integrations?: Array<{id:number;name:string;kind:string}> }>("/api/integrations", {}, workspace.id) : Promise.resolve({ integrations: [] as Array<{id:number;name:string;kind:string}> }),
    optionalServerApi<{ recordings?: Array<{id:number;filename:string;locale:string;status?:string;safetyStatus?:string;safety_status?:string}> }>("/api/recordings", {}, workspace.id),
  ]);
  const initialAgent = rawAgent ? normalizeAgent(rawAgent) : null;
  const integrationRows = integrationPayload.integrations || [];
  const recordingRows = (recordingPayload.recordings || []).filter(row => (!row.status || row.status === "ready") && (row.safetyStatus || row.safety_status) === "approved");
  const starterTemplate = params.new === "1" ? (typeof params.template === "string" ? params.template : "blank") : undefined;
  return <Studio workspace={{ id: workspace.id, name: workspace.name, role: workspace.role }} brand={brand} initialAgent={initialAgent} integrations={integrationRows} recordings={recordingRows} openGenerator={params.generate === "1"} starterTemplate={starterTemplate}/>;
}

type ProviderPolicy={llm:string[];stt:string[];tts:string[];realtime?:string[]};
type RawAgent={id:number;name:string;objective:string;global_prompt?:string;globalPrompt?:string;greeting:string;channel:string;model:string;voice:string;status:string;locale:string;provider_policy?:ProviderPolicy;providerPolicy?:ProviderPolicy;workflow:unknown;recording_enabled?:boolean;recordingEnabled?:boolean;max_call_seconds?:number;maxCallSeconds?:number;human_handoff_number?:string;humanHandoffNumber?:string};
function normalizeAgent(row:RawAgent){return{id:Number(row.id),name:row.name,objective:row.objective,globalPrompt:row.global_prompt??row.globalPrompt??row.objective,greeting:row.greeting,channel:row.channel,model:row.model,voice:row.voice,status:row.status,locale:row.locale,providerPolicy:row.provider_policy??row.providerPolicy??{llm:[],stt:[],tts:[]},workflow:row.workflow as never,recordingEnabled:row.recording_enabled??row.recordingEnabled??false,maxCallSeconds:Number(row.max_call_seconds??row.maxCallSeconds??1800),humanHandoffNumber:row.human_handoff_number??row.humanHandoffNumber??""}}
