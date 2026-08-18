"""
Thin read client for Meta's Instagram Insights API, using the per-business
token stored by app/instagram_insights_oauth.py.

Scope: this module ONLY reads raw Insights metrics (reach, saved,
engagement, impressions, etc.) for a connected business. It does not
interpret them -- no readiness-advisory scoring, no best-performer
ranking, no historical snapshotting. Those are separate, later pieces of
the ads engine that will consume this module's output; keeping them out
of here so this stays a pure, easily-testable data-access layer.

Every function returns None on any failure (not connected, expired token,
Graph API error) rather than raising -- callers are expected to treat
"no data yet" as a normal, expected state (same fail-safe convention as
app/engine/instagram_analysis.py's profile fetch).
"""
import logging
import uuid

import httpx

from app.config import settings
from app.db import get_session
from app.models import Business

logger = logging.getLogger("socioburp.engine.instagram_insights")

GRAPH_BASE = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}"

DEFAULT_ACCOUNT_METRICS = ["reach", "impressions", "profile_views"]
DEFAULT_MEDIA_METRICS = ["reach", "saved", "likes", "comments"]


def _get_connection(business_id: uuid.UUID) -> tuple[str, str] | None:
    """Returns (ig_user_id, access_token), or None if this business hasn't connected."""
    with get_session() as db:
        biz = db.query(Business).filter(Business.id == business_id).first()
        if biz is None or not biz.instagram_insights_ig_user_id or not biz.instagram_insights_access_token:
            return None
        return biz.instagram_insights_ig_user_id, biz.instagram_insights_access_token


async def get_account_insights(
    business_id: uuid.UUID,
    metrics: list[str] | None = None,
    period: str = "day",
) -> dict | None:
    """
    Account-level Insights (e.g. reach/impressions/profile_views over the
    given period). Returns the raw Graph API {"data": [...]} response, or
    None if not connected / the call failed.
    """
    connection = _get_connection(business_id)
    if connection is None:
        return None
    ig_user_id, access_token = connection
    metrics = metrics or DEFAULT_ACCOUNT_METRICS

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{GRAPH_BASE}/{ig_user_id}/insights",
                params={"metric": ",".join(metrics), "period": period, "access_token": access_token},
            )
        if resp.status_code >= 400:
            logger.warning(
                "Instagram account insights fetch failed for business=%s: %s | %s",
                business_id, resp.status_code, resp.text[:300],
            )
            return None
        return resp.json()
    except Exception:
        logger.exception("Instagram account insights fetch raised for business=%s", business_id)
        return None


async def get_media_insights(
    business_id: uuid.UUID,
    media_id: str,
    metrics: list[str] | None = None,
) -> dict | None:
    """
    Per-media Insights (reach/saved/likes/comments/etc. for one post).
    `media_id` is the Instagram media id (distinct from this app's own
    Generation.id) -- the caller is responsible for knowing which IG media
    a given piece of content maps to. Returns the raw Graph API
    {"data": [...]} response, or None if not connected / the call failed.
    """
    connection = _get_connection(business_id)
    if connection is None:
        return None
    _, access_token = connection
    metrics = metrics or DEFAULT_MEDIA_METRICS

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{GRAPH_BASE}/{media_id}/insights",
                params={"metric": ",".join(metrics), "access_token": access_token},
            )
        if resp.status_code >= 400:
            logger.warning(
                "Instagram media insights fetch failed for business=%s media=%s: %s | %s",
                business_id, media_id, resp.status_code, resp.text[:300],
            )
            return None
        return resp.json()
    except Exception:
        logger.exception("Instagram media insights fetch raised for business=%s media=%s", business_id, media_id)
        return None


def is_connected(business_id: uuid.UUID) -> bool:
    return _get_connection(business_id) is not None
