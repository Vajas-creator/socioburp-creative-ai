import type { NextRequest, NextResponse } from "next/server";
import { prisma } from "@/lib/prisma";
import type { User } from "@/generated/prisma/client";
import {
  ACCESS_COOKIE,
  REFRESH_COOKIE,
  accessTokenMaxAgeSeconds,
  generateOpaqueToken,
  hashOpaqueToken,
  refreshTokenExpiryDate,
  refreshTokenMaxAgeSeconds,
  signAccessToken,
} from "@/lib/auth";
import { getClientIp } from "@/lib/rate-limit";

const isProduction = process.env.NODE_ENV === "production";

/**
 * Issues a fresh access token + refresh token pair for a user, persists the
 * refresh token's hash (never the raw value) in the database, and writes
 * both as httpOnly cookies on the given response.
 */
export async function issueSession(
  response: NextResponse,
  user: Pick<User, "id" | "email" | "role" | "name">,
  request: NextRequest
): Promise<void> {
  const accessToken = await signAccessToken({
    sub: user.id,
    email: user.email,
    role: user.role,
    name: user.name,
  });

  const refreshToken = generateOpaqueToken();

  await prisma.refreshToken.create({
    data: {
      userId: user.id,
      tokenHash: hashOpaqueToken(refreshToken),
      expiresAt: refreshTokenExpiryDate(),
      createdByIp: getClientIp(request),
    },
  });

  setAuthCookies(response, accessToken, refreshToken);
}

export function setAuthCookies(
  response: NextResponse,
  accessToken: string,
  refreshToken: string
): void {
  response.cookies.set(ACCESS_COOKIE, accessToken, {
    httpOnly: true,
    secure: isProduction,
    sameSite: "lax",
    path: "/",
    maxAge: accessTokenMaxAgeSeconds(),
  });
  response.cookies.set(REFRESH_COOKIE, refreshToken, {
    httpOnly: true,
    secure: isProduction,
    sameSite: "lax",
    // Path must be "/", not scoped to /api/auth: src/proxy.ts needs this
    // cookie to be sent on /dashboard/** requests so it can silently
    // refresh an expired access token. httpOnly already prevents JS from
    // reading it regardless of path, so this doesn't add exposure.
    path: "/",
    maxAge: refreshTokenMaxAgeSeconds(),
  });
}

export function clearAuthCookies(response: NextResponse): void {
  response.cookies.set(ACCESS_COOKIE, "", { path: "/", maxAge: 0 });
  response.cookies.set(REFRESH_COOKIE, "", { path: "/", maxAge: 0 });
}

/** Revokes a refresh token by its raw (cookie) value, marking it used. */
export async function revokeRefreshToken(rawToken: string): Promise<void> {
  const tokenHash = hashOpaqueToken(rawToken);
  await prisma.refreshToken.updateMany({
    where: { tokenHash, revokedAt: null },
    data: { revokedAt: new Date() },
  });
}

/** Revokes every active refresh token for a user (e.g. after a password reset). */
export async function revokeAllRefreshTokensForUser(
  userId: string
): Promise<void> {
  await prisma.refreshToken.updateMany({
    where: { userId, revokedAt: null },
    data: { revokedAt: new Date() },
  });
}
