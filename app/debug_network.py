"""
TEMPORARY diagnostic endpoint — remove once the api.anthropic.com
connection issue (Aug 8, 2026) is resolved. Not meant to stay in
production long-term: it makes a real (tiny, ~5-token) live Anthropic API
call and exposes some environment/network detail.

Tests connectivity to api.anthropic.com at every layer, from lowest to
highest, so a single hit tells us exactly where it breaks instead of
guessing theory by theory:

  1. DNS resolution  — and which address families come back (confirms
     whether the IPv4-only patch in app/network_fix.py is actually active)
  2. Raw TCP connect  — bypasses httpx and the SDK entirely
  3. TLS handshake    — via a raw httpx GET, no Anthropic SDK involved
  4. Full Anthropic SDK call — the exact code path production uses

Each layer also runs the same test against a known-working comparison
host (graph.facebook.com, which has succeeded in every failure we've
seen so far) so we have a clean baseline in the same response.
"""
import asyncio
import logging
import secrets
import socket
import time

import httpx
from fastapi import APIRouter, HTTPException

from app.anthropic_client import client
from app.config import settings

logger = logging.getLogger("socioburp.debug_network")
router = APIRouter()


def _dns_test(host: str) -> dict:
    start = time.monotonic()
    try:
        results = socket.getaddrinfo(host, 443)
        families = sorted({r[0].name if hasattr(r[0], "name") else str(r[0]) for r in results})
        return {
            "ok": True,
            "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
            "families_returned": families,
            "resolved_addresses": [r[4][0] for r in results],
        }
    except Exception as e:
        return {"ok": False, "elapsed_ms": round((time.monotonic() - start) * 1000, 1), "error": f"{type(e).__name__}: {e}"}


async def _tcp_connect_test(host: str, port: int = 443, timeout: float = 8.0) -> dict:
    start = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        peer = writer.get_extra_info("peername")
        writer.close()
        await writer.wait_closed()
        return {"ok": True, "elapsed_ms": round((time.monotonic() - start) * 1000, 1), "connected_to": str(peer)}
    except Exception as e:
        return {"ok": False, "elapsed_ms": round((time.monotonic() - start) * 1000, 1), "error": f"{type(e).__name__}: {e}"}


async def _https_get_test(url: str, timeout: float = 8.0) -> dict:
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            resp = await http_client.get(url)
        return {"ok": True, "elapsed_ms": round((time.monotonic() - start) * 1000, 1), "status_code": resp.status_code}
    except Exception as e:
        return {"ok": False, "elapsed_ms": round((time.monotonic() - start) * 1000, 1), "error": f"{type(e).__name__}: {e}"}


async def _anthropic_sdk_test() -> dict:
    """Uses the SHARED client (app.anthropic_client), constructed once at
    module import time — outside any running event loop."""
    start = time.monotonic()
    try:
        response = await client.messages.create(
            model=settings.CLAUDE_INTENT_MODEL,
            max_tokens=5,
            messages=[{"role": "user", "content": "hi"}],
        )
        return {"ok": True, "elapsed_ms": round((time.monotonic() - start) * 1000, 1), "response_id": response.id}
    except Exception as e:
        return {"ok": False, "elapsed_ms": round((time.monotonic() - start) * 1000, 1), "error": f"{type(e).__name__}: {e}"}


async def _anthropic_sdk_fresh_client_test() -> dict:
    """
    Same call as _anthropic_sdk_test(), but constructs a BRAND NEW
    AsyncAnthropic (and its own fresh httpx client) right here, inside
    this async function, at request time — mirroring exactly how layer
    3's succeeding raw httpx test is structured. If this succeeds while
    _anthropic_sdk_test() (the shared, import-time-constructed client)
    fails, that isolates the problem to event-loop binding at
    construction time, not anything about the SDK or the network itself.
    """
    start = time.monotonic()
    try:
        from anthropic import AsyncAnthropic
        fresh_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = await fresh_client.messages.create(
            model=settings.CLAUDE_INTENT_MODEL,
            max_tokens=5,
            messages=[{"role": "user", "content": "hi"}],
        )
        return {"ok": True, "elapsed_ms": round((time.monotonic() - start) * 1000, 1), "response_id": response.id}
    except Exception as e:
        return {"ok": False, "elapsed_ms": round((time.monotonic() - start) * 1000, 1), "error": f"{type(e).__name__}: {e}"}


@router.get("/debug/network-check")
async def network_check(secret: str = ""):
    """
    Hit this directly from a browser or curl — gated by ?secret=, temporary
    only. Returns every layer's result for api.anthropic.com side-by-side
    with the same tests against graph.facebook.com (our known-working
    baseline).

    Fails closed: if DEBUG_NETWORK_SECRET isn't set, every request gets
    403 regardless of what ?secret= is — a missing env var must never
    accidentally leave this open, since it exposes internal network
    detail and makes a real billed Anthropic API call per hit.
    """
    if not settings.DEBUG_NETWORK_SECRET or not secrets.compare_digest(secret, settings.DEBUG_NETWORK_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")

    results = {
        "1_dns_resolution": {
            "anthropic": _dns_test("api.anthropic.com"),
            "facebook_baseline": _dns_test("graph.facebook.com"),
        },
    }

    results["2_raw_tcp_connect"] = {
        "anthropic": await _tcp_connect_test("api.anthropic.com"),
        "facebook_baseline": await _tcp_connect_test("graph.facebook.com"),
    }

    results["3_https_get_no_sdk"] = {
        "anthropic": await _https_get_test("https://api.anthropic.com/"),
        "facebook_baseline": await _https_get_test("https://graph.facebook.com/"),
    }

    results["4_full_anthropic_sdk_call_shared_client"] = await _anthropic_sdk_test()
    results["5_full_anthropic_sdk_call_fresh_client"] = await _anthropic_sdk_fresh_client_test()

    logger.info("Network diagnostic run: %s", results)
    return results
