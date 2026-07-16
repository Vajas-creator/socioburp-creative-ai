import { NextResponse, type NextRequest } from "next/server";
import { prisma } from "@/lib/prisma";
import {
  generateOpaqueToken,
  hashOpaqueToken,
  passwordResetExpiryDate,
} from "@/lib/auth";
import { forgotPasswordSchema } from "@/lib/validation";
import { checkRateLimit, getClientIp } from "@/lib/rate-limit";
import { sendPasswordResetEmail } from "@/lib/email";

const GENERIC_RESPONSE = {
  message:
    "If an account exists for that email, we've sent a password reset link.",
};

export async function POST(request: NextRequest) {
  const ip = getClientIp(request);
  const rateLimit = checkRateLimit(`forgot-password:${ip}`, 5, 15 * 60);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many requests. Please try again later." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  const body = await request.json().catch(() => null);
  const parsed = forgotPasswordSchema.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json({ error: "Invalid input" }, { status: 400 });
  }

  const { email } = parsed.data;
  const user = await prisma.user.findUnique({ where: { email } });

  // Always return the same generic response whether or not the account
  // exists, so this endpoint can't be used to enumerate registered emails.
  if (!user || !user.isActive) {
    return NextResponse.json(GENERIC_RESPONSE);
  }

  const rawToken = generateOpaqueToken();
  await prisma.passwordResetToken.create({
    data: {
      userId: user.id,
      tokenHash: hashOpaqueToken(rawToken),
      expiresAt: passwordResetExpiryDate(),
    },
  });

  const appUrl = process.env.APP_URL ?? request.nextUrl.origin;
  const resetUrl = `${appUrl}/reset-password?token=${rawToken}`;

  try {
    await sendPasswordResetEmail(user.email, resetUrl);
  } catch (err) {
    console.error("[forgot-password] Failed to send email:", err);
    // Still return the generic response — don't leak delivery failures.
  }

  return NextResponse.json(GENERIC_RESPONSE);
}
