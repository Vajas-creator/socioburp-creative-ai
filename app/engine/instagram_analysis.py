"""
Fetches a business's own Instagram profile content (bio + recent post
captions) so brand context/suggestions are actually grounded in what's on
their page, not just the bare handle they typed.

Previously Business.instagram_handle was stored and never read again by
anything -- see the Aug 2026 live-test report, item 7 ("does the bot
actually fetch and analyze the Instagram profile, or just store the
link?"). It was just the link.

Routed through a dedicated Make.com scenario ("SocioBurp — Instagram
Profile Fetch") that calls Meta's Business Discovery API using SocioBurp's
own connected Instagram Business account -- this only works for looking up
OTHER public Instagram Business/Creator accounts, so no OAuth or
connecting is needed from the client at all, unlike auto-posting (see
app/instagram.py) which does require the client's own account to be
authorized. A personal (non-Business/Creator) Instagram account won't
return anything here -- that's a real, expected gap, not a bug.

Best-effort only, always. Called fire-and-forget from onboarding.py (mirrors
the app/engine/industry_research.py background-fetch pattern) so it never
adds latency to the "give me a moment" -> first generation path -- the
first-ever generation won't have this yet, exactly like industry research
on a cache miss. Every failure mode (webhook not configured, non-200,
timeout, malformed response, handle isn't a public Business account)
degrades to "nothing fetched," never raises, never blocks anything.
"""
import logging
import re
import uuid

import httpx

from app.config import settings
from app.db import get_session
from app.models import BrandProfile

logger = logging.getLogger("socioburp.engine.instagram_analysis")

MAX_CAPTIONS = 5


def _normalize_handle(raw: str) -> str | None:
    """'https://instagram.com/xyz/', 'instagram.com/xyz', '@xyz', ' xyz ' -> 'xyz'."""
    text = (raw or "").strip()
    text = re.sub(r"^(https?://)?(www\.)?instagram\.com/", "", text, flags=re.IGNORECASE)
    text = text.strip("@/ ").split("?")[0].split("/")[0].strip()
    return text or None


async def fetch_profile_summary(handle: str) -> dict | None:
    """
    Returns {"biography": str | None, "recent_captions": list[str]} or None
    if there's nothing usable (fetch not configured, failed, or the account
    has no public bio/captions to show).
    """
    username = _normalize_handle(handle)
    if not username:
        return None

    if not settings.MAKE_INSTAGRAM_PROFILE_FETCH_WEBHOOK_URL:
        logger.info("MAKE_INSTAGRAM_PROFILE_FETCH_WEBHOOK_URL not configured — skipping Instagram content fetch")
        return None

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                settings.MAKE_INSTAGRAM_PROFILE_FETCH_WEBHOOK_URL,
                json={"username": username},
            )
        if resp.status_code != 200:
            logger.warning(
                "Instagram profile fetch for %r returned %s: %s",
                username, resp.status_code, resp.text[:300],
            )
            return None
        data = resp.json()
    except Exception:
        logger.exception("Instagram profile fetch failed for handle=%r", handle)
        return None

    biography = (data.get("biography") or "").strip() or None
    posts = data.get("recent_posts") or []
    captions = [
        p["caption"].strip()
        for p in posts
        if isinstance(p, dict) and isinstance(p.get("caption"), str) and p["caption"].strip()
    ][:MAX_CAPTIONS]

    if not biography and not captions:
        return None

    return {"biography": biography, "recent_captions": captions}


async def fetch_and_store_profile_summary(business_id: uuid.UUID, handle: str):
    """
    Fire-and-forget entry point -- see app/onboarding.py's
    asyncio.create_task() call. Opens its own DB session (the caller's has
    already closed by the time this actually runs) and writes straight to
    BrandProfile; a business that's since been deleted, or a profile row
    that's since disappeared, is a silent no-op, not an error.
    """
    result = await fetch_profile_summary(handle)
    if result is None:
        return

    with get_session() as db:
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == business_id).first()
        if profile is None:
            return
        profile.instagram_bio = result["biography"]
        profile.instagram_recent_captions = "\n".join(result["recent_captions"]) or None
