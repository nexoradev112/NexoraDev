import {getBrand,licensedServerApi,requireWorkspace} from "../../lib/server-api";
import RecordingsLibrary from "./recordings-library";

export const dynamic = "force-dynamic";

export default async function RecordingsPage() {
  const{workspace}=await requireWorkspace("/recordings");
  const[payload,brand]=await Promise.all([licensedServerApi<{recordings?:RawRecording[]}>("/api/recordings",workspace.id),getBrand()]);
  const rows=(payload.recordings||[]).map(row=>({id:Number(row.id),filename:row.filename,contentType:row.content_type??row.contentType??"audio/mpeg",size:Number(row.size||0),durationMs:Number(row.duration_ms??row.durationMs??0),transcript:row.transcript||"",locale:row.locale,status:row.status,safetyStatus:String(row.safety_status??row.safetyStatus??"pending_review"),createdAt:String(row.created_at??row.createdAt??"")}));
  return <RecordingsLibrary workspace={{id:workspace.id,name:workspace.name,plan:workspace.plan,role:workspace.role}} brand={brand} initialRecordings={rows.filter(row=>row.status==="ready")}/>;
}
type RawRecording={id:number;filename:string;content_type?:string;contentType?:string;size:number;duration_ms?:number;durationMs?:number;transcript?:string|null;locale:string;status:string;safety_status?:string;safetyStatus?:string;created_at?:string;createdAt?:string};
