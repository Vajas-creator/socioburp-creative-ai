"""
Single shared AsyncAnthropic client, reused across every module that calls
Claude, instead of each module creating its own independent client (and
therefore its own independent httpx connection pool).

Why this matters: this codebase grew from 4 modules calling Claude to 10
across recent development — each previously instantiated
`AsyncAnthropic(api_key=...)` at import time, meaning a single running
process held 10 separate connection pools. Consolidating to one shared,
reused client is simply correct SDK usage regardless of any other issue.

INVESTIGATION NOTE (Aug 8, 2026): production connection failures to
api.anthropic.com are under active investigation via GET
/debug/network-check (see app/debug_network.py). DNS, raw TCP, and a bare
httpx request all succeed cleanly against this host — only calls through
this module's client fail. An explicit http_client=... override was
tried and did NOT resolve it (ruled out — see git history on this file).
Current hypothesis under test: this client being constructed once at
MODULE IMPORT time, before uvicorn's event loop is running, may bind its
connection pool to the wrong loop. debug_network.py's layer 5
(_anthropic_sdk_fresh_client_test) constructs an equivalent client fresh,
inside a request handler, as a direct comparison. Don't change this
module's construction pattern again until that comparison gives a clear
answer — see the diagnostic result before attempting another fix here.

Import this shared `client` everywhere `AsyncAnthropic(...)` used to be
constructed locally: from app.anthropic_client import client
"""
from anthropic import AsyncAnthropic

from app.config import settings

client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
