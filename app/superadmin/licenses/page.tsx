import { notFound } from "next/navigation";
import { requireSession } from "../../../lib/server-api";
import LicenseConsole from "./license-console";

export const dynamic = "force-dynamic";

export default async function SuperadminLicensesPage() {
  const session = await requireSession("/superadmin/licenses");
  if (!session.user.isSuperadmin) notFound();
  return <LicenseConsole userName={session.user.name}/>;
}
