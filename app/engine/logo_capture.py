"""
Lets a client set/update their logo at any point in the conversation, not
just during onboarding -- see the Aug 2026 "there's no way to ever upload
a logo" investigation. app/storage.py's upload_logo() already existed
(left over from before onboarding was redesigned to drop the logo
question, see app/onboarding.py's docstring) but was never actually
called anywhere until this.

Entry point: app/router.py, for an image sent WITH a caption declaring it
as the logo ("this is my logo", "use this as my logo") -- see
app/engine/router_intent.py's LOGO_UPLOAD intent.

Any text alongside the image is stored verbatim as
BrandProfile.extras["logo_position_hint"] -- free-form, not parsed into a
fixed enum, since the whole point (see the "not template-y" Aug 2026
feedback) is that placement should be reasoned about per-image by
app/engine/logo_placement.py, not slotted into a handful of canned
positions. No stated preference is fine too -- logo_placement falls back
to picking whatever empty space looks best on its own.

Aug 2026 "white box overlapping my logo" + "why are my images dark navy
when my logo is white/sky blue" follow-up -- two things were missing from
this path even though both capabilities already existed elsewhere in the
codebase, just never wired in here:
  1. app/engine/logo_bg_removal.py strips a uniform (e.g. plain white)
     background before the logo is ever stored, so compositor.py's paste
     shows just the logo mark, not a visible rectangle of its background
     color -- see that module's docstring for the root cause.
  2. app/engine/color_discovery.py already extracts real brand colors
     from an image via Claude vision (used during onboarding's Instagram-
     screenshot step, app/onboarding.py), but a logo uploaded LATER,
     mid-conversation, never triggered it -- so a business that onboarded
     before uploading their real logo kept generating with whatever
     colors (or lack thereof) it started with, regardless of what their
     actual logo looks like. Now the same auto-apply-if-confident
     extraction runs on every logo upload, same as onboarding does with a
     screenshot, and (deliberately) overwrites any previously-set colors
     -- a client uploading their actual logo is the single most
     authoritative brand-color signal there is.
"""
import asyncio
import logging
import uuid

from app.db import get_session
from app.models import BrandProfile
from app.schemas import IncomingMessage
from app.whatsapp.client import send_text, download_media
from app.storage import upload_logo
from app.engine import logo_bg_removal, color_discovery
from app.image_utils import detect_image_media_type

logger = logging.getLogger("socioburp.engine.logo_capture")


def _save(business_id: uuid.UUID, logo_url: str, position_hint: str | None, colors: dict | None):
    with get_session() as db:
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == business_id).first()
        if profile is None:
            profile = BrandProfile(business_id=business_id)
            db.add(profile)
        profile.logo_url = logo_url
        if position_hint and position_hint.strip():
            extras = dict(profile.extras or {})
            extras["logo_position_hint"] = position_hint.strip()[:200]
            profile.extras = extras
        if colors and colors.get("confident") and colors.get("primary_color"):
            profile.primary_color = colors["primary_color"]
            profile.secondary_color = colors.get("secondary_color")


async def handle(business_id: uuid.UUID, msg: IncomingMessage):
    """Called from app/router.py when router_intent.classify() returns LOGO_UPLOAD."""
    phone = msg.sender

    if not (msg.type == "image" and msg.media_id):
        await send_text(phone, "Sure — just send me the logo image and I'll save it 🙂")
        return

    try:
        image_bytes = await download_media(msg.media_id)
        cleaned_bytes = logo_bg_removal.remove_uniform_background(image_bytes)
        logo_url = await asyncio.to_thread(upload_logo, business_id, cleaned_bytes)
    except Exception:
        logger.exception("Failed to save uploaded logo for business=%s", business_id)
        await send_text(phone, "Hmm, I couldn't quite save that logo 🙏 Could you try sending it again?")
        return

    # Best-effort: a failed/unconfident color read shouldn't block saving
    # the logo itself, so this runs after the logo is already safely
    # uploaded and any failure here is swallowed by _save()'s colors=None
    # handling (it just leaves existing colors untouched).
    colors = None
    try:
        media_type = detect_image_media_type(image_bytes)
        colors = await color_discovery.extract_colors_from_image(image_bytes, media_type=media_type)
    except Exception:
        logger.exception("Color extraction from uploaded logo failed for business=%s — logo saved anyway", business_id)

    _save(business_id, logo_url, msg.text, colors)

    colors_note = ""
    if colors and colors.get("confident") and colors.get("primary_color"):
        colors_note = f" Picked up your brand colors from it too ({colors['primary_color']}) — I'll use them from now on."

    await send_text(
        phone,
        "Got it, saved your logo! ✨ I'll place it thoughtfully on your creatives from now on"
        + (f" — noted you'd like it {msg.text.strip()}." if msg.text and msg.text.strip() else ".")
        + colors_note,
    )
