import { getBrand, requireWorkspace } from "../../lib/server-api";
import AnalyticsDashboard from "./analytics-dashboard";

export const dynamic="force-dynamic";
export default async function AnalyticsPage(){const{workspace}=await requireWorkspace("/analytics");return <AnalyticsDashboard workspace={{id:workspace.id,name:workspace.name,plan:workspace.plan,role:workspace.role}} brand={await getBrand()}/>}
