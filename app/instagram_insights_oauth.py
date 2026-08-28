"""
Per-business Instagram Insights OAuth (Facebook Login for Business).

Purely additive, and purely for READING a business's own Insights data
(reach, saves, engagement) for the ads engine's organic-performance
tracking -- see app/engine/instagram_insights.py for the actual read
client that uses the token stored here.

Deliberately NOT wired into, and does not touch, the existing Instagram
integrations:
  - app/instagram.py (auto-POSTING): routed through SocioBurp's own
    Make.com scenario + connected IG account, onboarded via a manual
    "add SocioBurp as Facebook Page admin" step. Keeps working exactly as
    it does today -- untouched by this module.
  - app/engine/instagram_analysis.py (public profile/caption fetch): uses
    Meta's Business Discovery API via SocioBurp's own connected account,
    no per-client OAuth at all.

Why this needs its own OAuth flow at all: Insights (reach, saves,
engagement) is PRIVATE data that only the account owner can grant access
to -- Business Discovery and Make's own connection can't see it. The
client authorizes SocioBurp's Meta app against their own Facebook Page +
linked Instagram Business/Creator account, granting instagram_basic +
instagram_manage_insights (+ pages_show_list/pages_read_engagement, since
an IG Business Account only exposes Insights via its linked Page).

SCOPES also includes instagram_content_publish. That grant isn't used by
anything in THIS file -- it exists so the page access token stored below
is also valid for app/engine/instagram_publish.py's native Content
Publishing API calls (POST .../media, .../media_publish), which reuses
this same connection rather than running a second OAuth flow. Businesses
that connected before this scope was added will need to reconnect
("connect instagram" again) before native publishing works for them.

Flow:
  1. Client texts something like "connect instagram" -> router.py calls
     send_connect_link() -> a signed, time-boxed link is sent on WhatsApp.
  2. GET /oauth/instagram/start verifies that link, then redirects to
     Meta's OAuth consent dialog, passing the same signed value through
     as `state`.
  3. Client approves on Meta's own page (never inside WhatsApp -- WhatsApp
     can't host an OAuth consent screen).
  4. GET /oauth/instagram/callback verifies `state`, exchanges the code
     for a short-lived token, exchanges that for a long-lived user token,
     looks up the Facebook Page (and its linked Instagram Business
     Account) the client just granted access to via /me/accounts, and
     stores the resulting page access token + IG user id on Business.
     Confirms back on WhatsApp; the browser gets a plain HTML page (this
     is a browser redirect target, not a JSON API).

State/link signing: HMAC-SHA256 over "<business_id>.<timestamp>" keyed by
META_APP_SECRET (already a server-held secret -- no new secret needed).
Time-boxed to LINK_TTL_SECONDS so a stale/leaked link can't be replayed
indefinitely. This is what stands in for "auth" on /start and /callback --
WhatsApp has no session/cookie to check, so only a link we ourselves
generated and sent to that business's own WhatsApp number is usable.
"""
import hashlib
import hmac
import logging
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.db import get_session
from app.models import Business
from app.whatsapp.client import send_text

logger = logging.getLogger("socioburp.instagram_insights_oauth")
router = APIRouter()

GRAPH_BASE = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}"
AUTH_DIALOG_BASE = f"https://www.facebook.com/{settings.META_GRAPH_API_VERSION}/dialog/oauth"
SCOPES = "instagram_basic,instagram_manage_insights,instagram_content_publish,pages_show_list,pages_read_engagement"
LINK_TTL_SECONDS = 30 * 60  # 30 minutes -- enough for a client to get through Meta's consent screen, incl. 2FA


# --- Signed token: doubles as both the WhatsApp link's auth and the OAuth `state` param ---

def _sign(business_id: uuid.UUID, ts: int) -> str:
    payload = f"{business_id}.{ts}"
    return hmac.new(settings.META_APP_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def _make_token(business_id: uuid.UUID) -> str:
    ts = int(time.time())
    return f"{business_id}.{ts}.{_sign(business_id, ts)}"


def _verify_token(token: str) -> uuid.UUID | None:
    try:
        biz_id_str, ts_str, sig = token.split(".", 2)
        business_id = uuid.UUID(biz_id_str)
        ts = int(ts_str)
    except (ValueError, AttributeError):
        return None

    if time.time() - ts > LINK_TTL_SECONDS:
        return None
    if not hmac.compare_digest(sig, _sign(business_id, ts)):
        return None
    return business_id


def _public_base_url() -> str:
    """Derived from META_OAUTH_REDIRECT_URI so there's one env var, not two, to keep in sync."""
    parts = urllib.parse.urlsplit(settings.META_OAUTH_REDIRECT_URI)
    return f"{parts.scheme}://{parts.netloc}"


# --- WhatsApp entry point ---

async def send_connect_link(business_id: uuid.UUID, phone: str):
    """Called from app/router.py on the 'connect_instagram' global command."""
    if not settings.META_APP_ID or not settings.META_OAUTH_REDIRECT_URI:
        logger.error("META_APP_ID/META_OAUTH_REDIRECT_URI not configured — cannot build Instagram connect link")
        await send_text(phone, "Performance tracking isn't set up yet on our end 🙏 We're on it.")
        return

    token = _make_token(business_id)
    link = f"{_public_base_url()}/oauth/instagram/start?token={token}"
    await send_text(
        phone,
        "Connect your Instagram to unlock performance tracking (reach, saves, engagement) 📊\n\n"
        f"{link}\n\n"
        "This link expires in 30 minutes and only works for your account. "
        "You'll be asked to log into Facebook and approve access to your "
        "Instagram Business/Creator account's insights.",
    )


# --- Graph API calls ---

async def _exchange_code_for_short_lived_token(code: str) -> str:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "redirect_uri": settings.META_OAUTH_REDIRECT_URI,
                "code": code,
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def _exchange_for_long_lived_token(short_lived_token: str) -> tuple[str, int]:
    """Returns (access_token, expires_in_seconds) -- typically ~60 days."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "fb_exchange_token": short_lived_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["access_token"], data.get("expires_in", 60 * 24 * 3600)


async def _find_ig_business_account(long_lived_user_token: str) -> dict | None:
    """
    Looks up the Facebook Pages this token's user just granted access to,
    and returns the first one with a linked Instagram Business/Creator
    account. Returns None if the client approved the dialog but their
    Instagram isn't actually a Business/Creator account linked to a Page
    they admin -- a real, expected gap (same caveat as Business Discovery
    in app/engine/instagram_analysis.py), not a bug.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/me/accounts",
            params={
                "fields": "id,name,access_token,instagram_business_account{id,username}",
                "access_token": long_lived_user_token,
            },
        )
        resp.raise_for_status()
        pages = resp.json().get("data", [])

    for page in pages:
        ig_account = page.get("instagram_business_account")
        if ig_account and ig_account.get("id"):
            return {
                "page_id": page["id"],
                "page_access_token": page["access_token"],
                "ig_user_id": ig_account["id"],
                "ig_username": ig_account.get("username"),
            }
    return None


_SUCCESS_HTML = """<!doctype html><html><body style="font-family:sans-serif;text-align:center;padding:60px 20px">
<h2>Instagram connected ✅</h2><p>You can close this tab and go back to WhatsApp.</p></body></html>"""

_FAILURE_HTML = """<!doctype html><html><body style="font-family:sans-serif;text-align:center;padding:60px 20px">
<h2>Something went wrong</h2><p>{message}</p><p>Go back to WhatsApp and try again.</p></body></html>"""


@router.get("/oauth/instagram/start")
async def oauth_start(token: str):
    business_id = _verify_token(token)
    if business_id is None:
        return HTMLResponse(_FAILURE_HTML.format(message="This link has expired or is invalid."), status_code=400)

    params = {
        "client_id": settings.META_APP_ID,
        "redirect_uri": settings.META_OAUTH_REDIRECT_URI,
        "state": token,  # re-verified as-is on callback -- same signed value, still within its TTL
        "scope": SCOPES,
        "response_type": "code",
    }
    return RedirectResponse(f"{AUTH_DIALOG_BASE}?{urllib.parse.urlencode(params)}")


@router.get("/oauth/instagram/callback")
async def oauth_callback(request: Request):
    query = request.query_params
    state = query.get("state", "")
    business_id = _verify_token(state)

    if query.get("error"):
        logger.info(
            "Instagram Insights OAuth denied/cancelled: %s | %s",
            query.get("error"), query.get("error_description"),
        )
        if business_id:
            with get_session() as db:
                biz = db.query(Business).filter(Business.id == business_id).first()
                phone = biz.phone if biz else None
            if phone:
                await send_text(phone, "Instagram connection cancelled — no changes made. Text 'connect instagram' anytime to try again.")
        return HTMLResponse(_FAILURE_HTML.format(message="Connection was cancelled."))

    if business_id is None:
        return HTMLResponse(_FAILURE_HTML.format(message="This link has expired or is invalid."), status_code=400)

    code = query.get("code")
    if not code:
        return HTMLResponse(_FAILURE_HTML.format(message="Missing authorization code."), status_code=400)

    with get_session() as db:
        biz = db.query(Business).filter(Business.id == business_id).first()
        phone = biz.phone if biz else None
    if not phone:
        return HTMLResponse(_FAILURE_HTML.format(message="Couldn't find your account."), status_code=400)

    try:
        short_lived = await _exchange_code_for_short_lived_token(code)
        long_lived, expires_in = await _exchange_for_long_lived_token(short_lived)
        ig_account = await _find_ig_business_account(long_lived)
    except Exception:
        logger.exception("Instagram Insights OAuth token exchange failed for business=%s", business_id)
        await send_text(phone, "Connecting Instagram failed 🙏 Please try again — text 'connect instagram'.")
        return HTMLResponse(_FAILURE_HTML.format(message="Token exchange with Meta failed."), status_code=502)

    if ig_account is None:
        await send_text(
            phone,
            "Couldn't find an Instagram Business/Creator account linked to a Facebook Page you manage 🙏 "
            "Make sure your Instagram is a Business or Creator account, connected to a Facebook Page you "
            "admin, then text 'connect instagram' to try again.",
        )
        return HTMLResponse(_FAILURE_HTML.format(message="No linked Instagram Business account found."))

    with get_session() as db:
        biz = db.query(Business).filter(Business.id == business_id).first()
        biz.instagram_insights_ig_user_id = ig_account["ig_user_id"]
        biz.instagram_insights_page_id = ig_account["page_id"]
        biz.instagram_insights_access_token = ig_account["page_access_token"]
        biz.instagram_insights_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        biz.instagram_insights_connected_at = datetime.now(timezone.utc)

    logger.info("Instagram Insights connected for business=%s ig_user_id=%s", business_id, ig_account["ig_user_id"])
    username_suffix = f" (@{ig_account['ig_username']})" if ig_account.get("ig_username") else ""
    await send_text(phone, f"Instagram connected{username_suffix} ✅ Performance tracking is live.")
    return HTMLResponse(_SUCCESS_HTML)
