import { NextResponse, type NextRequest } from "next/server";
import { prisma } from "@/lib/prisma";
import {
  ForbiddenError,
  UnauthorizedError,
  requireUser,
} from "@/lib/require-user";
import { STEP_SCHEMAS, isOnboardingStep } from "@/lib/onboarding";

/**
 * Saves one wizard step for the current user, creating the onboarding
 * record on first save. Each save is canonical for its step: optional
 * fields the user cleared are written back as NULL (the zod transforms
 * turn "" into null), so re-editing a step never leaves stale values.
 */
export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ step: string }> }
) {
  try {
    const user = await requireUser(request);

    const stepNumber = Number((await params).step);
    if (!isOnboardingStep(stepNumber)) {
      return NextResponse.json(
        { error: "Step must be a number from 1 to 5" },
        { status: 400 }
      );
    }

    const body = await request.json().catch(() => null);
    const parsed = STEP_SCHEMAS[stepNumber].safeParse(body);
    if (!parsed.success) {
      return NextResponse.json(
        { error: "Invalid input", details: parsed.error.flatten().fieldErrors },
        { status: 400 }
      );
    }

    // Replace undefined (key omitted by client) with null so a step save
    // always overwrites every field belonging to that step.
    const data = Object.fromEntries(
      Object.entries(parsed.data).map(([key, value]) => [key, value ?? null])
    );

    const onboarding = await prisma.clientOnboarding.upsert({
      where: { userId: user.sub },
      create: { userId: user.sub, ...data, lastCompletedStep: stepNumber },
      update: { ...data },
    });

    if (stepNumber > onboarding.lastCompletedStep) {
      await prisma.clientOnboarding.update({
        where: { userId: user.sub },
        data: { lastCompletedStep: stepNumber },
      });
      onboarding.lastCompletedStep = stepNumber;
    }

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
