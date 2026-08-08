"""
Industry style research — a one-time, cached, web-search-backed pass
distilling current visual/marketing trends for an industry, shared across
every business in that industry rather than re-run per client.

Deliberately NOT the "analyze this specific named competitor" idea from
earlier design discussion — that needs a real competitor's actual content,
which we can't get without scraping (ruled out) or the competitor's own
consent (unrealistic to ask for). This researches the INDUSTRY in general
— publicly available marketing/trend commentary, not any single business's
proprietary content — which is exactly what a web-search-backed Claude
call is legitimately good for.

Trigger: fired as a background asyncio task from onboarding.py right after
the client picks their industry, so it runs concurrently with the rest of
onboarding (logo, color, tone) rather than making them wait. By the time
onboarding finishes, the research is very likely already cached and ready
for their first generation. Only runs for concrete industries ("restaurant",
"salon") — skipped for "other", where there's no specific category to
research.
"""
import logging


from app.config import settings
from app.db import get_session
from app.models import IndustryStyleResearch

logger = logging.getLogger("socioburp.engine.industry_research")

from app.anthropic_client import create_message

RESEARCH_PROMPT_TEMPLATE = (
    "Research current Instagram marketing visual trends for {industry} "
    "businesses in India — color palettes, photography style, layout "
    "conventions, and what kind of posts tend to perform well right now. "
    "Search a few real sources, then write ONE short paragraph (3-5 "
    "sentences) synthesizing the pattern, written as direction for a "
    "graphic designer producing social posts for a small {industry} "
    "business. Reply with ONLY that paragraph — no preamble, no source "
    "list, no headers."
)


def get_cached_style(industry: str | None) -> str | None:
    """
    Read-side helper — used when building BusinessContext. Synchronous,
    fast, no API call — just a cache lookup.
    """
    if not industry:
        return None
    with get_session() as db:
        row = db.query(IndustryStyleResearch).filter(IndustryStyleResearch.industry == industry).first()
        return row.style_summary if row else None


async def research_and_cache_if_needed(industry: str | None):
    """
    Fire-and-forget: call via asyncio.create_task(), don't await inline in
    the onboarding conversation flow — a live web-search-backed Claude
    call can take several seconds, and there's no reason to make the
    client wait on it mid-onboarding. No-ops quietly for "other" (nothing
    concrete to research) or if already cached. Fails safe: any error is
    logged and swallowed — this is an enrichment, never something that
    should surface as a client-facing failure.
    """
    if not industry or industry == "other":
        return

    with get_session() as db:
        already_cached = db.query(IndustryStyleResearch).filter(IndustryStyleResearch.industry == industry).first()
    if already_cached:
        return

    try:
        response = await create_message(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=600,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": RESEARCH_PROMPT_TEMPLATE.format(industry=industry)}],
        )
        text_blocks = [block.text for block in response.content if getattr(block, "type", None) == "text"]
        style_summary = " ".join(t.strip() for t in text_blocks if t.strip())

        if not style_summary:
            logger.warning("Industry research for %r produced no text content — skipping cache write", industry)
            return

        with get_session() as db:
            existing = db.query(IndustryStyleResearch).filter(IndustryStyleResearch.industry == industry).first()
            if existing is None:
                db.add(IndustryStyleResearch(industry=industry, style_summary=style_summary))
                logger.info("Cached industry research for %r: %r", industry, style_summary[:120])

    except Exception:
        logger.exception("Industry research failed for %r — will retry on next uncached onboarding", industry)
