import AdminCMS from "./admin-cms";
import { notFound } from "next/navigation";
import { requireSession } from "../../lib/server-api";
export const dynamic="force-dynamic";
export default async function AdminPage(){
  const session=await requireSession("/admin");
  if(!session.user.isSuperadmin)notFound();
  return <AdminCMS/>;
}
