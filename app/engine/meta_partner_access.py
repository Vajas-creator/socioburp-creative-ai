"""
Thin write/read client for Meta's Marketing API "agencies" edge on a
client's ad account -- the mechanism an agency Business Manager (SocioBurp)
uses to request, and later verify, partner access on an ad account it does
NOT own, without ever needing the client's own login.

POST /act_{ad_account_id}/agencies (business=<SocioBurp's own Business ID>,
permitted_tasks=[...]), called with a SocioBurp System User token, creates
a PENDING partner request against that ad account -- it does not require
that ad account to belong to SocioBurp's Business Manager, or any prior
relationship with it. The client sees this as a pending partner request
under their own Business Settings -> Partners and must approve it there;
nothing here can complete that approval on their behalf.

GET on the same edge lists the businesses with (attempted) agency access
on that ad account and each one's access_status -- "CONFIRMED" once the
client has approved, still pending otherwise. That's the only source of
truth this module trusts; see app/engine/ad_account_connect.py, which
calls check_partner_access_status() to verify before ever marking a
business's partner_access_status as "granted".

Same fail-safe convention as app/engine/instagram_insights.py: every
function returns None on any failure (bad ID, expired/invalid token,
Graph API error) rather than raising -- a Marketing API hiccup should
never crash the WhatsApp conversation calling into this.

NOTE: written from Meta's documented Marketing API behavior for this edge
(POST creates a pending agency/partner request; GET's access_status field
distinguishes CONFIRMED from pending) -- verify the exact request/response
shape against the current Marketing API reference for the pinned
META_GRAPH_API_VERSION before this goes live with a real ad spend budget
behind it, since Meta revises Business Manager/Marketing API edges often.
"""
import logging
import re

import httpx

from app.config import settings

logger = logging.getLogger("socioburp.engine.meta_partner_access")

GRAPH_BASE = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}"

# What SocioBurp asks to be allowed to do on the client's ad account once
# access is granted -- running ads (ADVERTISE) and reading performance
# (ANALYZE). Deliberately NOT requesting MANAGE (full admin) -- an agency
# partner shouldn't need billing/account-settings control.
PERMITTED_TASKS = ["ADVERTISE", "ANALYZE"]

_DIGITS_RE = re.compile(r"^\d+$")


def normalize_ad_account_id(raw: str) -> str | None:
    """
    Accepts whatever a client types -- "act_123456789", "123456789", or
    with stray whitespace -- and returns the bare numeric ID the Graph API
    edge itself is built from (act_{id}), or None if it doesn't look like
    a real ad account ID at all.
    """
    candidate = raw.strip().lower().removeprefix("act_").strip()
    if _DIGITS_RE.match(candidate):
        return candidate
    return None


async def request_partner_access(ad_account_id: str) -> bool:
    """
    Sends the partner/agency access request from SocioBurp's own Business
    Manager to the given (client-owned) ad account. Returns True if Meta
    accepted the request (it will show as pending in the client's Business
    Settings), False on any failure -- a missing/misconfigured
    META_BUSINESS_ID/META_SYSTEM_USER_ACCESS_TOKEN, an invalid ad account
    ID, or a Graph API error.
    """
    if not settings.META_BUSINESS_ID or not settings.META_SYSTEM_USER_ACCESS_TOKEN:
        logger.error("META_BUSINESS_ID/META_SYSTEM_USER_ACCESS_TOKEN not configured — cannot request ad account partner access")
        return False

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{GRAPH_BASE}/act_{ad_account_id}/agencies",
                data={
                    "business": settings.META_BUSINESS_ID,
                    "permitted_tasks": str(PERMITTED_TASKS),
                    "access_token": settings.META_SYSTEM_USER_ACCESS_TOKEN,
                },
            )
        if resp.status_code != 200:
            logger.error(
                "Partner access request failed for ad_account_id=%s: %s %s",
                ad_account_id, resp.status_code, resp.text[:500],
            )
            return False
        return True
    except Exception:
        logger.exception("Partner access request errored for ad_account_id=%s", ad_account_id)
        return False


async def check_partner_access_status(ad_account_id: str) -> str | None:
    """
    Returns "CONFIRMED", "PENDING", or None (not found in the list at all,
    or the call itself failed -- callers should treat both the same: not
    yet granted). This is the only check app/engine/ad_account_connect.py
    trusts before marking a business's partner_access_status "granted" --
    the client's own "I approved it" message is never taken as sufficient
    on its own.
    """
    if not settings.META_BUSINESS_ID or not settings.META_SYSTEM_USER_ACCESS_TOKEN:
        logger.error("META_BUSINESS_ID/META_SYSTEM_USER_ACCESS_TOKEN not configured — cannot verify ad account partner access")
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{GRAPH_BASE}/act_{ad_account_id}/agencies",
                params={
                    "fields": "id,access_status",
                    "access_token": settings.META_SYSTEM_USER_ACCESS_TOKEN,
                },
            )
        if resp.status_code != 200:
            logger.error(
                "Partner access verification failed for ad_account_id=%s: %s %s",
                ad_account_id, resp.status_code, resp.text[:500],
            )
            return None

        entries = resp.json().get("data", [])
        for entry in entries:
            if str(entry.get("id")) == str(settings.META_BUSINESS_ID):
                return entry.get("access_status")
        return None
    except Exception:
        logger.exception("Partner access verification errored for ad_account_id=%s", ad_account_id)
        return None
