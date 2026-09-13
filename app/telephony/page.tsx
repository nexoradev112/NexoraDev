import {getBrand,licensedServerApi,optionalServerApi,requireWorkspace} from "../../lib/server-api";
import { notFound } from "next/navigation";
import TelephonyConsole from "./telephony-console";

export const dynamic = "force-dynamic";

export default async function TelephonyPage(){const{workspace}=await requireWorkspace("/telephony");if(!["admin","owner"].includes(workspace.role))notFound();const[connectionPayload,numberPayload,brand]=await Promise.all([licensedServerApi<{connections?:RawConnection[]}>("/api/telephony",workspace.id),optionalServerApi<{phoneNumbers?:RawPhone[];phone_numbers?:RawPhone[]}>("/api/phone-numbers",{},workspace.id),getBrand()]);const connections=(connectionPayload.connections||[]).map(row=>({...row,updatedAt:String(row.updated_at??row.updatedAt??"")}));const numbers=(numberPayload.phoneNumbers||numberPayload.phone_numbers||[]).map(row=>({...row,createdAt:String(row.created_at??row.createdAt??"")}));return <TelephonyConsole workspace={{id:workspace.id,name:workspace.name,plan:workspace.plan,role:workspace.role}} brand={brand} initialConnections={connections} initialNumbers={numbers.filter(row=>row.status==="active")}/>}
type RawConnection={id:number;provider:string;label:string;config:Record<string,string>;status:string;updated_at?:string;updatedAt?:string};
type RawPhone={id:number;provider:string;providerRef?:string;e164:string;label:string;direction:string;status:string;created_at?:string;createdAt?:string};
