"""
Onboarding state machine. One question per message, state persisted on the
Business row so a user can go silent mid-flow and resume later without
losing progress.

States: new -> name -> industry -> logo -> colors -> tone -> done
"""
import logging
import uuid

from app.db import get_session
from app.models import Business, BrandProfile
from app.schemas import IncomingMessage
from app.whatsapp.client import send_text, send_buttons, download_media
from app.storage import upload_logo
from app.config import settings

logger = logging.getLogger("socioburp.onboarding")

INDUSTRY_BUTTONS = [("restaurant", "Restaurant"), ("salon", "Salon/Beauty"), ("other", "Other")]
TONE_BUTTONS = [("premium", "Premium"), ("friendly", "Friendly"), ("bold", "Bold")]


async def advance(business_id: uuid.UUID, msg: IncomingMessage):
    with get_session() as db:
        biz = db.query(Business).filter(Business.id == business_id).first()
        state = biz.onboarding_state
        phone = biz.phone

        # Ensure a brand_profiles row exists once we start needing it
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == business_id).first()
        if profile is None:
            profile = BrandProfile(business_id=business_id)
            db.add(profile)

        if state == "new":
            await send_text(
                phone,
                "👋 Welcome to SocioBurp! I'll help you create branded social media "
                "content in seconds.\n\nFirst — what's your business name?",
            )
            biz.onboarding_state = "awaiting_name"
            return

        if state == "awaiting_name":
            if not msg.text:
                await send_text(phone, "Please type your business name as text 🙂")
                return
            biz.name = msg.text.strip()
            await send_buttons(phone, "Great! What type of business is it?", INDUSTRY_BUTTONS)
            biz.onboarding_state = "awaiting_industry"
            return

        if state == "awaiting_industry":
            industry = msg.button_id or (msg.text or "").strip().lower()
            if industry not in ("restaurant", "salon", "other"):
                await send_buttons(phone, "Please pick one of the options below:", INDUSTRY_BUTTONS)
                return
            biz.industry = industry
            await send_text(
                phone,
                "Perfect. Now send me your logo as an image 📎\n\n"
                "(Or type 'skip' if you don't have one handy — you can add it later)",
            )
            biz.onboarding_state = "awaiting_logo"
            return

        if state == "awaiting_logo":
            if msg.type == "image" and msg.media_id:
                try:
                    image_bytes = await download_media(msg.media_id)
                    logo_url = upload_logo(business_id, image_bytes)
                    profile.logo_url = logo_url
                    await send_text(phone, "Logo saved! ✅")
                except Exception:
                    logger.exception("Logo upload failed for business=%s", business_id)
                    await send_text(phone, "Hmm, couldn't save that logo. Let's continue without it for now.")
            elif (msg.text or "").strip().lower() == "skip":
                await send_text(phone, "No problem, you can add a logo anytime later.")
            else:
                await send_text(phone, "Please send your logo as an image, or type 'skip'.")
                return

            await send_text(
                phone,
                "What's your main brand color? Send a hex code like #E91E63 "
                "(or type 'skip' if you're not sure)",
            )
            biz.onboarding_state = "awaiting_color"
            return

        if state == "awaiting_color":
            text = (msg.text or "").strip()
            if text.lower() == "skip":
                pass
            elif text.startswith("#") and len(text) == 7:
                profile.primary_color = text.upper()
            else:
                await send_text(phone, "That doesn't look like a hex color. Try e.g. #E91E63, or type 'skip'.")
                return

            await send_buttons(phone, "Last question — what's your brand vibe?", TONE_BUTTONS)
            biz.onboarding_state = "awaiting_tone"
            return

        if state == "awaiting_tone":
            tone = msg.button_id or (msg.text or "").strip().lower()
            if tone not in ("premium", "friendly", "bold"):
                await send_buttons(phone, "Please pick one of the options below:", TONE_BUTTONS)
                return
            profile.tone = tone
            biz.onboarding_state = "done"

            # Signup bonus
            from app.credits import add_credits
            add_credits(db, business_id, settings.SIGNUP_BONUS_CREDITS, reason="signup_bonus")

            await send_text(
                phone,
                f"🎉 You're all set! You have {settings.SIGNUP_BONUS_CREDITS} free credits.\n\n"
                "Try: *Create a weekend offer post*\n\n"
                "Anytime, you can also type:\n"
                "• *credits* — check your balance\n"
                "• *history* — see recent creatives\n"
                "• *topup* — buy more credits",
            )
            return

        # Fallback — shouldn't normally hit this
        logger.warning("Unknown onboarding state '%s' for business=%s", state, business_id)
        biz.onboarding_state = "new"
