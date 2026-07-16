import { NextResponse, type NextRequest } from "next/server";
import { prisma } from "@/lib/prisma";
import { verifyPassword } from "@/lib/auth";
import { issueSession } from "@/lib/tokens";
import { loginSchema } from "@/lib/validation";
import { checkRateLimit, getClientIp } from "@/lib/rate-limit";

const GENERIC_ERROR = "Invalid email or password.";

export async function POST(request: NextRequest) {
  const ip = getClientIp(request);
  const rateLimit = checkRateLimit(`login:${ip}`, 10, 15 * 60);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many login attempts. Please try again later." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  const body = await request.json().catch(() => null);
  const parsed = loginSchema.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json({ error: "Invalid input" }, { status: 400 });
  }

  const { email, password } = parsed.data;

  const user = await prisma.user.findUnique({ where: { email } });
  // Always run bcrypt.compare, even for a nonexistent user, against a
  // constant dummy hash so response timing doesn't leak whether the email
  // is registered.
  const passwordHash =
    user?.passwordHash ??
    "$2a$12$CwTycUXWue0Thq9StjUM0uJ8lqPd0hHfGh4WwYQpJ0F2sD3AZ9v5W";
  const passwordValid = await verifyPassword(password, passwordHash);

  if (!user || !passwordValid || !user.isActive) {
    return NextResponse.json({ error: GENERIC_ERROR }, { status: 401 });
  }

  const response = NextResponse.json({
    user: { id: user.id, name: user.name, email: user.email, role: user.role },
  });

  await issueSession(response, user, request);

  return response;
}
