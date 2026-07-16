import { cookies } from "next/headers";
import { ACCESS_COOKIE, verifyAccessToken, type AccessTokenPayload } from "@/lib/auth";

/**
 * Reads and verifies the access token cookie in a Server Component / layout.
 * Returns null if missing or invalid — callers decide whether that means
 * "redirect to login" or "render as logged out".
 *
 * Note: this only checks the short-lived access token, not the refresh
 * token, so it can be called without hitting the database. The `proxy.ts`
 * proxy is what actually redirects unauthenticated requests before they
 * reach these components; this is a second, defense-in-depth check.
 */
export async function getCurrentUser(): Promise<AccessTokenPayload | null> {
  const cookieStore = await cookies();
  const token = cookieStore.get(ACCESS_COOKIE)?.value;
  if (!token) return null;
  return verifyAccessToken(token);
}
