"""
Single shared AsyncAnthropic client, reused across every module that calls
Claude, instead of each module creating its own independent client (and
therefore its own independent httpx connection pool).

Why this matters: this codebase grew from 4 modules calling Claude to 10
across recent development — each previously instantiated
`AsyncAnthropic(api_key=...)` at import time, meaning a single running
process held 10 separate connection pools. Consolidating to one shared,
reused client is simply correct SDK usage regardless of any other issue.

IMPORTANT (Aug 8, 2026): explicitly passes our own pre-configured httpx
client instead of letting the SDK build its own internally. Diagnosed via
GET /debug/network-check on production: DNS, raw TCP, and a bare
`httpx.AsyncClient()` HTTPS request ALL succeeded cleanly against
api.anthropic.com (410ms, clean response) — but the Anthropic SDK's own
internal call to the exact same host still failed with
APIConnectionError, taking 1.5s to do so. That isolates the problem to
something specific in how the SDK constructs its OWN httpx client,
separate from DNS/IPv6/general network reachability (all already ruled
out — see app/network_fix.py and app/debug_network.py). Since a bare
httpx client is proven to work against this exact host, handing the SDK
that same kind of client directly sidesteps whatever it was doing
differently on its own.

Import this shared `client` everywhere `AsyncAnthropic(...)` used to be
constructed locally: from app.anthropic_client import client
"""
import httpx
from anthropic import AsyncAnthropic

from app.config import settings

_http_client = httpx.AsyncClient(timeout=60.0)

client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY, http_client=_http_client)
