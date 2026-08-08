"""
Single shared AsyncAnthropic client, reused across every module that calls
Claude, instead of each module creating its own independent client (and
therefore its own independent httpx connection pool).

Why this matters: this codebase grew from 4 modules calling Claude to 10+
across recent development — each previously instantiated
`AsyncAnthropic(api_key=...)` at import time, meaning a single running
process held that many separate connection pools. Consolidating to one
shared, reused client is simply correct SDK usage regardless of any other
issue.

INVESTIGATION HISTORY (Aug 8, 2026): production connection failures to
api.anthropic.com were investigated via GET /debug/network-check (see
app/debug_network.py). DNS, raw TCP, and a bare httpx request all succeeded
cleanly against this host while calls through this module's client failed.
An earlier attempt at an explicit http_client=... override (default httpx
settings) was tried and did NOT resolve it on its own. This revision
reapplies an explicit http_client, but now with the specific transport
settings called out in that investigation (HTTP/1.1 only, no env-derived
proxy/trust config, explicit connect/read timeouts, and a bounded
connection pool) rather than httpx's defaults — narrower and more
deliberate than the reverted attempt. It is paired with retry-on-
APIConnectionError (below) so that even if this doesn't fully resolve the
underlying cause, transient failures don't surface to users.

Import this shared `client` everywhere `AsyncAnthropic(...)` used to be
constructed locally: from app.anthropic_client import client

For actually issuing a message request, prefer `create_message(**kwargs)`
below over `client.messages.create(**kwargs)` directly — it adds retry
logic scoped specifically to APIConnectionError, with richer failure
logging, so every call site gets the same resilience without duplicating
the retry loop 13 times over.
"""
import asyncio
import logging

import httpx
from anthropic import APIConnectionError, AsyncAnthropic

from app.config import settings

logger = logging.getLogger("socioburp.anthropic_client")

# Single reused httpx client, constructed once here at import time and
# passed into the Anthropic client below — NOT created per-request. A
# fresh client per call would open (and never reuse) its own connection
# pool per request, which is both wasteful and one of the hypotheses this
# investigation was trying to rule out.
_http_client = httpx.AsyncClient(
    http2=False,
    trust_env=False,
    timeout=httpx.Timeout(30.0, connect=10.0),
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)

client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY, http_client=_http_client)

_RETRY_ATTEMPTS = 3
_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)  # backoff between attempts; only the first (_RETRY_ATTEMPTS - 1) are ever used at _RETRY_ATTEMPTS=3


async def create_message(**kwargs):
    """
    Shared entry point for every Claude call in this codebase. Wraps
    client.messages.create(**kwargs) with retry logic scoped ONLY to
    APIConnectionError (3 attempts total, backoff 1s/2s/4s between
    retries) — any other exception (bad request, auth, rate limit,
    overloaded, etc.) propagates immediately on the first attempt, since
    retrying those wouldn't help and each call site's own except block
    already handles them (fallback content, logging, etc.).
    """
    last_exc = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return await client.messages.create(**kwargs)
        except APIConnectionError as e:
            last_exc = e
            logger.error(
                "Anthropic call failed (attempt %d/%d): %s, cause: %r",
                attempt + 1, _RETRY_ATTEMPTS, e, e.__cause__, exc_info=True,
            )
            if attempt < _RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_RETRY_DELAYS_SECONDS[attempt])
    raise last_exc
