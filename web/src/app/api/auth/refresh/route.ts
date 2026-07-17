import { NextResponse, type NextRequest } from "next/server";
import { prisma } from "@/lib/prisma";
import {
  REFRESH_COOKIE,
  generateOpaqueToken,
  hashOpaqueToken,
  refreshTokenExpiryDate,
  signAccessToken,
} from "@/lib/auth";
import { clearAuthCookies, setAuthCookies } from "@/lib/tokens";
import { getClientIp } from "@/lib/rate-limit";

/**
 * Rotates the refresh token on every use: the old one is marked revoked and
 * linked to its replacement, and a new access + refresh pair is issued. If
 * a refresh token is presented twice (revoked-but-reused), that's a strong
 * signal of theft, so we revoke the entire chain and force re-login.
 */
export async function POST(request: NextRequest) {
  const rawToken = request.cookies.get(REFRESH_COOKIE)?.value;
  if (!rawToken) {
    return NextResponse.json({ error: "Not authenticated" }, { status: 401 });
  }

  const tokenHash = hashOpaqueToken(rawToken);
  const existing = await prisma.refreshToken.findUnique({
    where: { tokenHash },
    include: { user: true },
  });

  if (!existing) {
    const response = NextResponse.json({ error: "Invalid session" }, { status: 401 });
    clearAuthCookies(response);
    return response;
  }

  if (existing.revokedAt || existing.expiresAt < new Date()) {
    // Reuse of a revoked token (or an expired one) — kill every session for
    // this user as a precaution and require a fresh login.
    await prisma.refreshToken.updateMany({
      where: { userId: existing.userId, revokedAt: null },
      data: { revokedAt: new Date() },
    });
    const response = NextResponse.json({ error: "Session expired" }, { status: 401 });
    clearAuthCookies(response);
    return response;
  }

  if (!existing.user.isActive) {
    const response = NextResponse.json({ error: "Account disabled" }, { status: 403 });
    clearAuthCookies(response);
    return response;
  }

  const newRawToken = generateOpaqueToken();
  const newTokenHash = hashOpaqueToken(newRawToken);

  await prisma.$transaction([
    prisma.refreshToken.update({
      where: { id: existing.id },
      data: { revokedAt: new Date() },
    }),
    prisma.refreshToken.create({
      data: {
        userId: existing.userId,
        tokenHash: newTokenHash,
        expiresAt: refreshTokenExpiryDate(),
        createdByIp: getClientIp(request),
      },
    }),
  ]);

  const accessToken = await signAccessToken({
    sub: existing.user.id,
    email: existing.user.email,
    role: existing.user.role,
    name: existing.user.name,
  });

  const response = NextResponse.json({ ok: true });
  setAuthCookies(response, accessToken, newRawToken);
  return response;
}
