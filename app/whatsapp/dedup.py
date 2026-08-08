"""
Dedup for Meta webhook redeliveries.

app/whatsapp/webhook.py's own comment documents the risk: if our POST
/webhook response takes too long, Meta retries the SAME message. The
per-business lock in app/router.py fixes CONCURRENT processing of two
DIFFERENT messages — it does nothing here, since a redelivery arrives as
its own separate webhook call and processes fully "correctly" a second
time by the lock's own logic. Concretely this can double-charge a credit,
send a duplicate image, or (during onboarding) advance the state machine
an extra step on one real answer.

Fix: track Meta's message ID (WAMID) for a short window and skip anything
already seen. In-process, in-memory — correct for the current single
Render instance. If this ever scales to multiple instances/processes,
this needs to become a shared store (e.g. a small Postgres table or
Redis) instead, same caveat as the per-business lock in app/router.py.
"""
import asyncio
import time

_seen_message_ids: dict[str, float] = {}
_dedup_lock = asyncio.Lock()

# Generous margin over Meta's actual retry window (typically well under a
# minute) — err toward remembering too long rather than too short.
DEDUP_WINDOW_SECONDS = 600


async def is_duplicate(message_id: str | None) -> bool:
    """
    Returns True if this message_id was already seen within the dedup
    window (skip processing — it's a Meta redelivery). Returns False and
    marks it seen otherwise. Also opportunistically evicts expired
    entries so this dict doesn't grow unboundedly — no separate
    background task needed given current message volume.

    A missing message_id fails OPEN (returns False, processes normally)
    rather than blocking a real message just because something upstream
    didn't provide an ID — dedup is a safety net, not a gate.
    """
    if not message_id:
        return False

    now = time.monotonic()
    async with _dedup_lock:
        expired = [mid for mid, seen_at in _seen_message_ids.items() if now - seen_at > DEDUP_WINDOW_SECONDS]
        for mid in expired:
            del _seen_message_ids[mid]

        if message_id in _seen_message_ids:
            return True

        _seen_message_ids[message_id] = now
        return False
