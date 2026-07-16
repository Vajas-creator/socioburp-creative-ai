import { NextResponse, type NextRequest } from "next/server";
import {
  ACCESS_COOKIE,
  REFRESH_COOKIE,
  verifyAccessToken,
  type AccessTokenPayload,
} from "@/lib/auth";

/**
 * Route guard for /dashboard/**. Runs on the Node runtime (default since
 * Next.js 16), so it can afford a same-origin fetch to /api/auth/refresh
 * for a transparent silent-refresh when the short-lived access token has
 * expired but the refresh token is still valid.
 *
 * This is defense-in-depth, not the only check — every Server Function and
 * Route Handler re-verifies the caller independently (see src/lib/session.ts
 * and the `requireUser`/`requireRole` helpers used in API routes), per
 * Next.js's own guidance that Proxy alone shouldn't be trusted for auth.
 */
export async function proxy(request: NextRequest) {
  const accessToken = request.cookies.get(ACCESS_COOKIE)?.value;
  const payload = accessToken ? await verifyAccessToken(accessToken) : null;

  if (payload) {
    return guardOrNext(request, payload, request.headers);
  }

  const refreshToken = request.cookies.get(REFRESH_COOKIE)?.value;
  if (refreshToken) {
    const refreshed = await tryRefresh(request);
    if (refreshed) return refreshed;
  }

  const loginUrl = new URL("/login", request.url);
  loginUrl.searchParams.set("next", request.nextUrl.pathname);
  return NextResponse.redirect(loginUrl);
}

/** Redirects away from admin-only paths for non-admins; otherwise continues
 * the request, forwarding the given headers to the downstream render. */
function guardOrNext(
  request: NextRequest,
  payload: AccessTokenPayload,
  forwardHeaders: Headers
): NextResponse {
  if (isAdminOnlyPath(request.nextUrl.pathname) && payload.role !== "ADMIN") {
    return NextResponse.redirect(new URL("/dashboard", request.url));
  }
  return NextResponse.next({ request: { headers: forwardHeaders } });
}

function isAdminOnlyPath(pathname: string): boolean {
  return pathname.startsWith("/dashboard/admin");
}

async function tryRefresh(request: NextRequest): Promise<NextResponse | null> {
  try {
    const refreshResponse = await fetch(
      new URL("/api/auth/refresh", request.url),
      {
        method: "POST",
        headers: { cookie: request.headers.get("cookie") ?? "" },
      }
    );

    if (!refreshResponse.ok) return null;

    const setCookies = refreshResponse.headers.getSetCookie();
    if (setCookies.length === 0) return null;

    const newAccessToken = extractCookieValue(setCookies, ACCESS_COOKIE);
    const newPayload = newAccessToken
      ? await verifyAccessToken(newAccessToken)
      : null;
    if (!newPayload) return null;

    // Rewrite the *incoming request's* Cookie header with the freshly
    // minted tokens, not just the outgoing response's Set-Cookie — Server
    // Components rendered for this same request read cookies off the
    // request, and Set-Cookie alone only affects what the browser stores
    // for the *next* request.
    const requestHeaders = new Headers(request.headers);
    const cookieJar = parseCookieHeader(requestHeaders.get("cookie") ?? "");
    for (const setCookie of setCookies) {
      const [name, value] = splitSetCookiePair(setCookie);
      if (name) cookieJar.set(name, value);
    }
    requestHeaders.set(
      "cookie",
      Array.from(cookieJar, ([name, value]) => `${name}=${value}`).join("; ")
    );

    const response = guardOrNext(request, newPayload, requestHeaders);
    for (const cookie of setCookies) {
      response.headers.append("set-cookie", cookie);
    }
    return response;
  } catch {
    return null;
  }
}

function extractCookieValue(setCookies: string[], name: string): string | null {
  for (const setCookie of setCookies) {
    const [cookieName, value] = splitSetCookiePair(setCookie);
    if (cookieName === name) return value;
  }
  return null;
}

function splitSetCookiePair(setCookie: string): [string | null, string] {
  const pair = setCookie.split(";")[0];
  const eq = pair.indexOf("=");
  if (eq === -1) return [null, ""];
  return [pair.slice(0, eq).trim(), pair.slice(eq + 1).trim()];
}

function parseCookieHeader(header: string): Map<string, string> {
  const jar = new Map<string, string>();
  for (const part of header.split(";")) {
    const eq = part.indexOf("=");
    if (eq === -1) continue;
    jar.set(part.slice(0, eq).trim(), part.slice(eq + 1).trim());
  }
  return jar;
}

export const config = {
  matcher: ["/dashboard/:path*"],
};
