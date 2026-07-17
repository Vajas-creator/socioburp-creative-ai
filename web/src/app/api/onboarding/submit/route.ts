import { NextResponse, type NextRequest } from "next/server";
import { prisma } from "@/lib/prisma";
import {
  ForbiddenError,
  UnauthorizedError,
  requireUser,
} from "@/lib/require-user";
import { REQUIRED_STEPS, STEP_SCHEMAS, STEP_TITLES } from "@/lib/onboarding";

/**
 * Final submit from the Review & Finish step. Re-validates the stored data
 * for every required step against the same zod schemas used at save time
 * (defense in depth — the client can't skip a step by jumping straight to
 * submit), then stamps completedAt. Editing later and resubmitting simply
 * refreshes the stamp.
 */
export async function POST(request: NextRequest) {
  try {
    const user = await requireUser(request);

    const onboarding = await prisma.clientOnboarding.findUnique({
      where: { userId: user.sub },
    });
    if (!onboarding) {
      return NextResponse.json(
        { error: "Start the onboarding wizard before submitting." },
        { status: 400 }
      );
    }

    const incompleteSteps: string[] = [];
    for (const step of REQUIRED_STEPS) {
      const result = STEP_SCHEMAS[step].safeParse(onboarding);
      if (!result.success) {
        incompleteSteps.push(`Step ${step}: ${STEP_TITLES[step]}`);
      }
    }
    if (incompleteSteps.length > 0) {
      return NextResponse.json(
        {
          error: "Some required steps are incomplete.",
          incompleteSteps,
        },
        { status: 400 }
      );
    }

    const updated = await prisma.clientOnboarding.update({
      where: { userId: user.sub },
      data: { completedAt: new Date() },
    });

    return NextResponse.json({ onboarding: updated });
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
