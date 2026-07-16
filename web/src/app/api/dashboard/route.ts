import { NextResponse, type NextRequest } from "next/server";
import { getDashboardData } from "@/lib/dashboard-data";
import {
  ForbiddenError,
  UnauthorizedError,
  requireUser,
} from "@/lib/require-user";

/**
 * Backend endpoint backing the dashboard, for programmatic/external
 * consumers (the dashboard page itself calls getDashboardData directly as
 * a Server Component to skip the network hop). Admins get org-wide user
 * data; everyone else gets their own profile. All data is read live from
 * Postgres — nothing here is mocked.
 */
export async function GET(request: NextRequest) {
  try {
    const user = await requireUser(request);
    const data = await getDashboardData(user);
    return NextResponse.json(data);
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      return NextResponse.json({ error: err.message }, { status: 401 });
    }
    if (err instanceof ForbiddenError) {
      return NextResponse.json({ error: err.message }, { status: 403 });
    }
    throw err;
  }
}
