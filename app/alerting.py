"""
Telegram-based failure alerting -- Priority 8 of the Aug 2026 consolidated
fix list. Render's free tier has no shell access for tailing logs, so an
unhandled error or a failed generation otherwise stays silent in logs
until a client complains. send_alert() fires immediately, in-process, from
inside the existing except blocks that already catch these failures --
no separate worker, cron, or polling loop needed.

Builds on ALERT_TELEGRAM_TOKEN / ALERT_TELEGRAM_CHAT_ID (app/config.py) --
defined for a while but never wired to anything until now.

Per-`kind` cooldown (COOLDOWN_SECONDS) keeps one bad deploy or a full
outage from turning into dozens of duplicate pings: the first failure of a
given kind alerts immediately, repeats of that SAME kind are suppressed
until the cooldown passes, but a DIFFERENT kind of failure alerts right
away regardless. In-memory only -- resets on process restart, which is
fine here, since under-alerting-after-a-restart is a far cheaper failure
mode than the dict growing unbounded or needing its own persistence.
"""
import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger("socioburp.alerting")

COOLDOWN_SECONDS = 900  # 15 min

_last_sent: dict[str, float] = {}


async def send_alert(kind: str, message: str):
    """
    Fire-and-forget: never raises. Safe to call from inside any except
    block without risking that a broken alert path masks or replaces the
    original error / the user-facing reply already being sent.
    """
    if not settings.ALERT_TELEGRAM_TOKEN or not settings.ALERT_TELEGRAM_CHAT_ID:
        return

    now = time.monotonic()
    last = _last_sent.get(kind)
    if last is not None and (now - last) < COOLDOWN_SECONDS:
        return
    _last_sent[kind] = now

    try:
        url = f"https://api.telegram.org/bot{settings.ALERT_TELEGRAM_TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json={
                "chat_id": settings.ALERT_TELEGRAM_CHAT_ID,
                "text": f"\U0001F6A8 SocioBurp alert [{kind}]\n\n{message}",
            })
    except Exception:
        logger.exception("Failed to send Telegram alert for kind=%s", kind)
