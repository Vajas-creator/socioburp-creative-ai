import { NextResponse, type NextRequest } from "next/server";
import { prisma } from "@/lib/prisma";
import {
  ForbiddenError,
  UnauthorizedError,
  requireUser,
} from "@/lib/require-user";

/** Returns the current user's onboarding record, or null if not started. */
export async function GET(request: NextRequest) {
  try {
    const user = await requireUser(request);
    const onboarding = await prisma.clientOnboarding.findUnique({
      where: { userId: user.sub },
    });
    return NextResponse.json({ onboarding });
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
