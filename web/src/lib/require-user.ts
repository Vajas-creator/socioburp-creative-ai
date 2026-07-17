import type { NextRequest } from "next/server";
import { ACCESS_COOKIE, verifyAccessToken, type AccessTokenPayload } from "@/lib/auth";
import type { Role } from "@/generated/prisma/client";

/** Verifies the access token cookie on an API request. Null if absent/invalid. */
export async function getUserFromRequest(
  request: NextRequest
): Promise<AccessTokenPayload | null> {
  const token = request.cookies.get(ACCESS_COOKIE)?.value;
  if (!token) return null;
  return verifyAccessToken(token);
}

export class UnauthorizedError extends Error {}
export class ForbiddenError extends Error {}

/** Throws UnauthorizedError/ForbiddenError; callers map those to 401/403 responses. */
export async function requireUser(
  request: NextRequest,
  allowedRoles?: Role[]
): Promise<AccessTokenPayload> {
  const user = await getUserFromRequest(request);
  if (!user) throw new UnauthorizedError("Not authenticated");
  if (allowedRoles && !allowedRoles.includes(user.role)) {
    throw new ForbiddenError("Insufficient permissions");
  }
  return user;
}
