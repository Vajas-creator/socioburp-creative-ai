"""
Instagram account linking via Facebook Login for Business OAuth.

Flow: a WhatsApp user types "connect instagram" -> we send them a link to
Meta's OAuth dialog (state carries their phone number) -> they authorize in
their browser -> Meta redirects to our /instagram/oauth/callback with a
`code` -> we exchange it for a long-lived Page access token, find the
connected Instagram professional account, save it, and confirm over
WhatsApp — mirroring the existing onboarding/payments modules' pattern of
"WhatsApp message in, WhatsApp message out", not a parallel session store.

Docs: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login
"""
import logging
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.config import settings
from app.db import get_session
from app.models import Business, InstagramConnection
from app.whatsapp.client import send_text

logger = logging.getLogger("socioburp.instagram_oauth")
router = APIRouter()

GRAPH_BASE = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}"

# Standard scope set for linking + reading insights + publishing to an
# Instagram professional account via its connected Facebook Page.
IG_SCOPES = (
    "instagram_basic,instagram_manage_insights,instagram_content_publish,"
    "pages_show_list,business_management"
)

WA_STATE_PREFIX = "wa:"
# The reviewer-demo secret travels inside `state`, not as a sibling query
# param: Meta's OAuth dialog only reliably round-trips `code` and `state`
# back to the registered redirect_uri. Appending our own extra params to
# redirect_uri at authorize-request time either gets stripped or, in
# stricter Facebook Login product modes, rejected outright as a
# redirect_uri mismatch — so `state` is the one place we can safely smuggle
# app-defined data through the whole trip.
APP_REVIEW_DEMO_STATE_PREFIX = "app_review_demo:"


class InstagramOAuthError(Exception):
    """Raised for any step of the code-exchange / account-lookup failing."""


def instagram_oauth_configured() -> bool:
    return bool(
        settings.META_APP_ID and settings.META_APP_SECRET and settings.META_OAUTH_REDIRECT_URI
    )


def build_instagram_oauth_url(state: str) -> str:
    """Build the Facebook Login for Business OAuth dialog URL."""
    query = urlencode(
        {
            "client_id": settings.META_APP_ID,
            "redirect_uri": settings.META_OAUTH_REDIRECT_URI,
            "scope": IG_SCOPES,
            "response_type": "code",
            "state": state,
        }
    )
    return f"https://www.facebook.com/{settings.META_GRAPH_API_VERSION}/dialog/oauth?{query}"


def build_app_review_demo_url() -> str:
    """
    One-off link to hand to a Meta App Reviewer (paste into the "Instructions
    to reproduce" field). Requires META_APP_REVIEW_DEMO_ENABLED=true and
    APP_REVIEW_DEMO_TOKEN to be set — raises otherwise, since a demo link
    generated without a real token would just 404 at the reviewer.
    """
    if not settings.APP_REVIEW_DEMO_TOKEN:
        raise InstagramOAuthError(
            "APP_REVIEW_DEMO_TOKEN is not configured; set it before generating a reviewer link."
        )
    return build_instagram_oauth_url(
        state=f"{APP_REVIEW_DEMO_STATE_PREFIX}{settings.APP_REVIEW_DEMO_TOKEN}"
    )


def _demo_access_allowed(state: str) -> bool:
    """True only if the demo flag is on, a real token is configured, the
    state carries the demo prefix, AND the embedded token matches — the
    fixed prefix string alone is never sufficient."""
    if not settings.META_APP_REVIEW_DEMO_ENABLED or not settings.APP_REVIEW_DEMO_TOKEN:
        return False
    if not state.startswith(APP_REVIEW_DEMO_STATE_PREFIX):
        return False
    provided = state[len(APP_REVIEW_DEMO_STATE_PREFIX):]
    return secrets.compare_digest(provided, settings.APP_REVIEW_DEMO_TOKEN)


async def send_instagram_connect_link(business_id, phone: str) -> None:
    """Entry point for the WhatsApp 'connect instagram' keyword (see app/router.py)."""
    if not instagram_oauth_configured():
        await send_text(
            phone,
            "📸 Instagram linking is being set up — check back soon!",
        )
        return

    link = build_instagram_oauth_url(state=f"{WA_STATE_PREFIX}{phone}")
    await send_text(
        phone,
        "📸 Tap the link below to connect your Instagram account:\n\n"
        f"{link}\n\n"
        "You'll be asked to log in to Facebook and choose the Page linked "
        "to your Instagram professional account.",
    )


async def exchange_code_for_page_and_ig_account(code: str) -> dict:
    """
    Real Graph API exchange: authorization code -> short-lived user token ->
    long-lived user token -> the first connected Page + Instagram Business
    Account reachable by this user. Raises InstagramOAuthError on any
    failure, with the underlying reason logged server-side.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        short_lived = await client.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "redirect_uri": settings.META_OAUTH_REDIRECT_URI,
                "code": code,
            },
        )
        if short_lived.status_code >= 400:
            logger.error("IG token exchange failed: %s | %s", short_lived.status_code, short_lived.text)
            raise InstagramOAuthError("Could not exchange the authorization code for a token.")
        short_lived_token = short_lived.json()["access_token"]

        long_lived = await client.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "fb_exchange_token": short_lived_token,
            },
        )
        if long_lived.status_code >= 400:
            logger.error("IG long-lived token exchange failed: %s | %s", long_lived.status_code, long_lived.text)
            raise InstagramOAuthError("Could not obtain a long-lived access token.")
        long_lived_token = long_lived.json()["access_token"]

        pages_resp = await client.get(
            f"{GRAPH_BASE}/me/accounts",
            params={
                "fields": "id,name,access_token,instagram_business_account{id,username}",
                "access_token": long_lived_token,
            },
        )
        if pages_resp.status_code >= 400:
            logger.error("IG pages lookup failed: %s | %s", pages_resp.status_code, pages_resp.text)
            raise InstagramOAuthError("Could not list your Facebook Pages.")

        pages = pages_resp.json().get("data", [])
        for page in pages:
            ig_account = page.get("instagram_business_account")
            if ig_account:
                return {
                    "page_id": page["id"],
                    "page_access_token": page["access_token"],
                    "ig_user_id": ig_account["id"],
                    "ig_username": ig_account.get("username", ""),
                }

    raise InstagramOAuthError(
        "No Instagram professional account is connected to any of your Facebook Pages."
    )


def save_connection(business_id, account: dict) -> None:
    with get_session() as db:
        existing = (
            db.query(InstagramConnection)
            .filter(InstagramConnection.business_id == business_id)
            .first()
        )
        if existing:
            existing.ig_user_id = account["ig_user_id"]
            existing.ig_username = account["ig_username"]
            existing.page_id = account["page_id"]
            existing.access_token = account["page_access_token"]
            existing.scopes = IG_SCOPES
        else:
            db.add(
                InstagramConnection(
                    business_id=business_id,
                    ig_user_id=account["ig_user_id"],
                    ig_username=account["ig_username"],
                    page_id=account["page_id"],
                    access_token=account["page_access_token"],
                    scopes=IG_SCOPES,
                )
            )


def _html(message: str) -> HTMLResponse:
    return HTMLResponse(f"<html><body style='font-family:sans-serif'>{message}</body></html>")


@router.get("/instagram/oauth/callback")
async def instagram_oauth_callback(code: str, state: str):
    try:
        account = await exchange_code_for_page_and_ig_account(code)
    except InstagramOAuthError as e:
        logger.warning("Instagram OAuth failed for state=%s: %s", state, e)
        if state.startswith(WA_STATE_PREFIX):
            phone = state.removeprefix(WA_STATE_PREFIX)
            await send_text(
                phone,
                "⚠️ We couldn't connect your Instagram account. Please make sure it's a "
                "professional account linked to a Facebook Page, then try again.",
            )
        return _html(f"<h2>&#9888; Could not connect Instagram</h2><p>{e}</p>")

    if state.startswith(APP_REVIEW_DEMO_STATE_PREFIX):
        if not _demo_access_allowed(state):
            return _html("<h2>&#9888; This link is not currently active.</h2>")
        # Reviewer path — the OAuth handshake above is real (the reviewer's
        # own account was really authorized), but nothing is persisted and
        # no WhatsApp message is sent since there's no associated business.
        return _html(
            f"<h2>&#9989; Instagram account @{account['ig_username']} linked</h2>"
            f"<p>Account ID: {account['ig_user_id']}</p>"
        )

    if not state.startswith(WA_STATE_PREFIX):
        return _html("<h2>&#9888; Invalid or expired link.</h2>")

    phone = state.removeprefix(WA_STATE_PREFIX)
    with get_session() as db:
        business = db.query(Business).filter(Business.phone == phone).first()
        if business is None:
            return _html(
                "<h2>&#9888; We couldn't find your SocioBurp account.</h2>"
                "<p>Message the WhatsApp bot to get started first, then try connecting Instagram again.</p>"
            )
        business_id = business.id

    save_connection(business_id, account)
    await send_text(phone, f"✅ Instagram account @{account['ig_username']} linked")
    return _html(
        f"<h2>&#9989; Instagram account @{account['ig_username']} linked</h2>"
        "<p>Head back to WhatsApp to keep going.</p>"
    )
