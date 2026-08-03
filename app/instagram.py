"""
Instagram auto-posting, routed through a Make.com scenario
(see: https://eu1.make.com scenario id 6803417, "SocioBurp — Instagram Auto-Post").

Flow: client taps "Post to Instagram" on a delivered creative -> router.py
catches the button_id -> handle_post_request() here looks up the generation
+ the business's linked Instagram account, calls the Make webhook, and
confirms back on WhatsApp.

Each business is mapped to a Meta Instagram Business Account ID via
Business.instagram_account_id. NULL means the business hasn't been
onboarded for auto-posting yet — onboarding is currently a manual, two-sided
step (client adds SocioBurp as Facebook Page admin; we look up their
Instagram account ID via Make's "Pages" RPC on our connection and store it
here). No Make-scenario edit is needed per client — the scenario reads the
target account dynamically from the payload.
"""
import logging
import uuid

import httpx

from app.config import settings
from app.db import get_session
from app.models import Business, Generation
from app.whatsapp.client import send_text

logger = logging.getLogger("socioburp.instagram")


async def handle_post_request(business_id: uuid.UUID, phone: str, generation_id: uuid.UUID):
    """
    Called when a client taps the "Post to Instagram" button on a delivered
    creative (button_id format: post_ig_<generation_id>, parsed in router.py).
    """
    with get_session() as db:
        business = db.query(Business).filter(Business.id == business_id).first()
        gen = db.query(Generation).filter(
            Generation.id == generation_id,
            Generation.business_id == business_id,  # never let a business post another's creative
        ).first()

        # Pull everything into plain values while the session is still open —
        # gen/business become unusable (DetachedInstanceError) once we leave
        # this block, since get_session() expires attributes on commit.
        gen_found = gen is not None
        already_posted = gen.posted_to_instagram if gen else None
        image_url = gen.image_url if gen else None
        caption = gen.caption if gen else None
        hashtags = gen.hashtags if gen else None
        instagram_account_id = business.instagram_account_id if business else None

    if not gen_found:
        await send_text(phone, "Couldn't find that creative — it may be too old. Please generate a new one.")
        return

    if already_posted:
        await send_text(phone, "That one's already posted to Instagram ✅")
        return

    if not instagram_account_id:
        await send_text(
            phone,
            "Your Instagram isn't connected for auto-posting yet 🙏 "
            "Reach out to your SocioBurp contact to get it set up.",
        )
        return

    if not settings.MAKE_INSTAGRAM_WEBHOOK_URL:
        logger.error("MAKE_INSTAGRAM_WEBHOOK_URL not configured — cannot post generation=%s", generation_id)
        await send_text(phone, "Posting to Instagram isn't set up yet on our end 🙏 We're on it.")
        return

    full_caption = f"{caption}\n\n{hashtags}" if caption else ""

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                settings.MAKE_INSTAGRAM_WEBHOOK_URL,
                json={
                    "account_id": instagram_account_id,
                    "content_type": "photo",
                    "image_url": image_url,
                    "caption": full_caption[:2200],  # Instagram caption limit
                },
            )
        if resp.status_code >= 400:
            logger.error("Make IG webhook failed: %s | %s", resp.status_code, resp.text)
            await send_text(phone, "Posting to Instagram failed 🙏 No credits affected — please try again.")
            return
    except Exception:
        logger.exception("Instagram post request failed for generation=%s", generation_id)
        await send_text(phone, "Posting to Instagram failed 🙏 No credits affected — please try again.")
        return

    with get_session() as db:
        gen_row = db.query(Generation).filter(Generation.id == generation_id).first()
        gen_row.posted_to_instagram = True

    await send_text(phone, "Posted to Instagram ✅")
