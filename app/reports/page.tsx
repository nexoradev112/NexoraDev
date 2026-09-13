import {getBrand,licensedServerApi,requireWorkspace} from "../../lib/server-api";
import DailyReports from "./daily-reports";

export const dynamic="force-dynamic";
export default async function ReportsPage(){const{workspace}=await requireWorkspace("/reports");const[payload,brand]=await Promise.all([licensedServerApi<{agents?:Array<{id:number;name:string}>}>("/api/agents",workspace.id),getBrand()]);return <DailyReports workspace={{id:workspace.id,name:workspace.name,plan:workspace.plan,role:workspace.role}} brand={brand} agents={payload.agents||[]}/>}
