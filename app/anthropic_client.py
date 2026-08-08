"""
Single shared AsyncAnthropic client, reused across every module that calls
Claude, instead of each module creating its own independent client (and
therefore its own independent httpx connection pool).

Why this matters: this codebase grew from 4 modules calling Claude to 10
across recent development — each previously instantiated
`AsyncAnthropic(api_key=...)` at import time, meaning a single running
process held 10 separate connection pools. On a resource-constrained
instance this is a plausible contributor to intermittent
APIConnectionError failures (seen in production Aug 8, 2026, first real
traffic after a large merge that added six of these ten call sites at
once). Even setting that specific incident aside, holding one shared,
reused client is simply correct SDK usage — the Anthropic Python SDK is
explicitly designed to be instantiated once and reused, not recreated per
call site — so this is worth doing regardless of whether it turns out to
be the whole explanation.

Import this shared `client` everywhere `AsyncAnthropic(...)` used to be
constructed locally: from app.anthropic_client import client
"""
from anthropic import AsyncAnthropic

from app.config import settings

client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
