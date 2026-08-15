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
"""
import asyncio
import logging
import uuid

from app.db import get_session
from app.models import BrandProfile
from app.schemas import IncomingMessage
from app.whatsapp.client import send_text, download_media
from app.storage import upload_logo

logger = logging.getLogger("socioburp.engine.logo_capture")


def _save(business_id: uuid.UUID, logo_url: str, position_hint: str | None):
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


async def handle(business_id: uuid.UUID, msg: IncomingMessage):
    """Called from app/router.py when router_intent.classify() returns LOGO_UPLOAD."""
    phone = msg.sender

    if not (msg.type == "image" and msg.media_id):
        await send_text(phone, "Sure — just send me the logo image and I'll save it 🙂")
        return

    try:
        image_bytes = await download_media(msg.media_id)
        logo_url = await asyncio.to_thread(upload_logo, business_id, image_bytes)
    except Exception:
        logger.exception("Failed to save uploaded logo for business=%s", business_id)
        await send_text(phone, "Hmm, I couldn't quite save that logo 🙏 Could you try sending it again?")
        return

    _save(business_id, logo_url, msg.text)

    await send_text(
        phone,
        "Got it, saved your logo! ✨ I'll place it thoughtfully on your creatives from now on"
        + (f" — noted you'd like it {msg.text.strip()}." if msg.text and msg.text.strip() else "."),
    )
