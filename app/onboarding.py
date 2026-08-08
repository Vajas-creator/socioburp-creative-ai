"""
Onboarding state machine. One question per message, state persisted on the
Business row so a user can go silent mid-flow and resume later without
losing progress.

States: new -> awaiting_name -> awaiting_industry -> awaiting_logo ->
        awaiting_color_screenshot -> [awaiting_color_confirm] -> awaiting_color_manual
        -> awaiting_tone -> done

Color discovery (Aug 2026): replaces the old "type a hex code" question.
Ask for an IG screenshot (or logo already on file) -> Claude vision
extracts candidate brand colors -> client explicitly confirms or rejects
(never auto-applied — see app/engine/color_discovery.py for why). 'skip'
or a rejected suggestion falls through to the original manual hex-code
question, so nothing is lost versus the old flow.

Industry research (Aug 2026): fired as a background asyncio task right
after industry is selected, NOT awaited inline — a live web-search-backed
Claude call can take several seconds and there's no reason to block the
conversation on it. By the time onboarding finishes (logo, color, tone
still to go), the research is very likely already cached. See
app/engine/industry_research.py.

Language: on the very first contact (state == "new"), whatever the client
typed to trigger this conversation gets run through i18n.detect_language()
before the welcome message is sent.
"""
import asyncio
import logging
import uuid

from app.db import get_session
from app.models import Business, BrandProfile
from app.schemas import IncomingMessage
from app.whatsapp.client import send_text, send_buttons, download_media
from app.storage import upload_logo
from app.config import settings
from app import i18n
from app.engine import industry_research, color_discovery
from app.engine import intent as intent_engine

logger = logging.getLogger("socioburp.onboarding")

INDUSTRY_BUTTONS = [("restaurant", "Restaurant"), ("salon", "Salon/Beauty"), ("other", "Other")]
TONE_BUTTONS = [("premium", "Premium"), ("friendly", "Friendly"), ("bold", "Bold")]
COLOR_CONFIRM_BUTTONS = [("yes_colors", "Yes, that's right"), ("no_colors", "No, let me specify")]

LANGUAGE_OVERRIDE_KEYWORDS = {
    "english": "en", "hindi": "hi", "hinglish": "hinglish",
    "tamil": "ta", "telugu": "te", "kannada": "kn", "malayalam": "ml",
}


async def advance(business_id: uuid.UUID, msg: IncomingMessage):
    with get_session() as db:
        biz = db.query(Business).filter(Business.id == business_id).first()
        state = biz.onboarding_state
        phone = biz.phone
        language = biz.preferred_language or "en"

        text_lower = (msg.text or "").strip().lower()
        if text_lower in LANGUAGE_OVERRIDE_KEYWORDS:
            new_lang = LANGUAGE_OVERRIDE_KEYWORDS[text_lower]
            biz.preferred_language = new_lang
            confirm = await i18n.t(
                "language_switched", new_lang,
                "Switched to {language_name} ✅",
                language_name=i18n.LANGUAGE_NAMES[new_lang],
            )
            await send_text(phone, confirm)
            return

        profile = db.query(BrandProfile).filter(BrandProfile.business_id == business_id).first()
        if profile is None:
            profile = BrandProfile(business_id=business_id)
            db.add(profile)

        if state == "new":
            detected = await i18n.detect_language(msg.text)
            biz.preferred_language = detected
            language = detected

            # If the very first message already describes a real creative
            # request ("Create a Diwali offer post, 20% off") rather than
            # just a greeting ("hi"), don't force the generic "what's your
            # business name?" opener with no acknowledgment of what they
            # actually asked for. We still need name/industry/etc. before a
            # *good* creative can be produced, so the question sequence
            # itself is unchanged -- but the request is remembered
            # (Business.pending_first_request) and auto-generated the
            # moment onboarding finishes, so the client never has to repeat
            # themselves. See the "awaiting_tone" branch below.
            is_direct_request = False
            if msg.text and msg.text.strip():
                intent_result = await intent_engine.classify(msg.text)
                is_direct_request = intent_result["intent"] == "GENERATE"

            if is_direct_request:
                biz.pending_first_request = msg.text.strip()
                welcome = await i18n.t(
                    "welcome_direct_request", language,
                    "👋 Hi, I'm Maya — your creative partner at SocioBurp! Got it, I can make "
                    "that for you 🎉\n\nQuick setup first (30 seconds) — what's your business name?",
                )
            else:
                welcome = await i18n.t(
                    "welcome", language,
                    "👋 Hi, I'm Maya — your creative partner at SocioBurp! I'll help you create "
                    "branded social media content in seconds.\n\nFirst — what's your business name?",
                )
            if language != "en":
                note = await i18n.t(
                    "language_note", language,
                    "(Replying in {language_name} — type 'english' anytime to switch.)",
                    language_name=i18n.LANGUAGE_NAMES[language],
                )
                welcome = f"{welcome}\n\n{note}"
            await send_text(phone, welcome)
            biz.onboarding_state = "awaiting_name"
            return

        if state == "awaiting_name":
            if not msg.text:
                msg_text = await i18n.t("name_needs_text", language, "Please type your business name as text 🙂")
                await send_text(phone, msg_text)
                return
            biz.name = msg.text.strip()
            prompt = await i18n.t("ask_industry", language, "Great! What type of business is it?")
            await send_buttons(phone, prompt, INDUSTRY_BUTTONS)
            biz.onboarding_state = "awaiting_industry"
            return

        if state == "awaiting_industry":
            industry = msg.button_id or (msg.text or "").strip().lower()
            if industry not in ("restaurant", "salon", "other"):
                prompt = await i18n.t("pick_option", language, "Please pick one of the options below:")
                await send_buttons(phone, prompt, INDUSTRY_BUTTONS)
                return
            biz.industry = industry

            # Fire-and-forget — runs concurrently with the rest of onboarding,
            # never blocks this reply. No-ops internally for "other" or if
            # already cached.
            asyncio.create_task(industry_research.research_and_cache_if_needed(industry))

            ask_logo = await i18n.t(
                "ask_logo", language,
                "Perfect. Now send me your logo as an image 📎\n\n"
                "(Or type 'skip' if you don't have one handy — you can add it later)",
            )
            await send_text(phone, ask_logo)
            biz.onboarding_state = "awaiting_logo"
            return

        if state == "awaiting_logo":
            if msg.type == "image" and msg.media_id:
                try:
                    image_bytes = await download_media(msg.media_id)
                    logo_url = upload_logo(business_id, image_bytes)
                    profile.logo_url = logo_url
                    saved = await i18n.t("logo_saved", language, "Logo saved! ✅")
                    await send_text(phone, saved)
                except Exception:
                    logger.exception("Logo upload failed for business=%s", business_id)
                    failed = await i18n.t(
                        "logo_failed", language,
                        "Hmm, couldn't save that logo. Let's continue without it for now.",
                    )
                    await send_text(phone, failed)
            elif text_lower == "skip":
                skipped = await i18n.t("logo_skipped", language, "No problem, you can add a logo anytime later.")
                await send_text(phone, skipped)
            else:
                retry = await i18n.t("logo_retry", language, "Please send your logo as an image, or type 'skip'.")
                await send_text(phone, retry)
                return

            ask_screenshot = await i18n.t(
                "ask_color_screenshot", language,
                "Now, send me a screenshot of your Instagram profile (or your website) "
                "and I'll figure out your brand colors from it 🎨\n\n"
                "(Or type 'skip' to enter a hex code manually instead)",
            )
            await send_text(phone, ask_screenshot)
            biz.onboarding_state = "awaiting_color_screenshot"
            return

        if state == "awaiting_color_screenshot":
            if text_lower == "skip":
                ask_hex = await i18n.t(
                    "ask_color_manual", language,
                    "No problem — what's your main brand color? Send a hex code like "
                    "#E91E63 (or type 'skip' if you're not sure)",
                )
                await send_text(phone, ask_hex)
                biz.onboarding_state = "awaiting_color_manual"
                return

            if msg.type == "image" and msg.media_id:
                try:
                    image_bytes = await download_media(msg.media_id)
                except Exception:
                    logger.exception("Screenshot download failed for business=%s", business_id)
                    image_bytes = None

                extracted = await color_discovery.extract_colors_from_image(image_bytes) if image_bytes else None

                if extracted and extracted.get("confident") and extracted.get("primary_color"):
                    profile.primary_color = extracted["primary_color"]
                    profile.secondary_color = extracted.get("secondary_color")
                    secondary_note = f" and {extracted['secondary_color']}" if extracted.get("secondary_color") else ""
                    confirm_msg = await i18n.t(
                        "color_confirm", language,
                        "Based on that, I'm seeing {primary}{secondary_note} as your brand colors. Sound right?",
                        primary=extracted["primary_color"], secondary_note=secondary_note,
                    )
                    await send_buttons(phone, confirm_msg, COLOR_CONFIRM_BUTTONS)
                    biz.onboarding_state = "awaiting_color_confirm"
                    return

                # Extraction failed or wasn't confident — fall through to manual, same as 'skip'
                unclear = await i18n.t(
                    "color_unclear", language,
                    "I couldn't confidently pick out brand colors from that 🙏 What's your "
                    "main brand color? Send a hex code like #E91E63 (or type 'skip')",
                )
                await send_text(phone, unclear)
                biz.onboarding_state = "awaiting_color_manual"
                return

            retry = await i18n.t(
                "color_screenshot_retry", language,
                "Please send a screenshot as an image, or type 'skip'.",
            )
            await send_text(phone, retry)
            return

        if state == "awaiting_color_confirm":
            choice = msg.button_id or text_lower
            if choice == "yes_colors":
                ask_tone = await i18n.t("ask_tone", language, "Last question — what's your brand vibe?")
                await send_buttons(phone, ask_tone, TONE_BUTTONS)
                biz.onboarding_state = "awaiting_tone"
                return
            if choice == "no_colors":
                ask_hex = await i18n.t(
                    "ask_color_manual", language,
                    "No problem — what's your main brand color? Send a hex code like "
                    "#E91E63 (or type 'skip' if you're not sure)",
                )
                await send_text(phone, ask_hex)
                biz.onboarding_state = "awaiting_color_manual"
                return
            prompt = await i18n.t("pick_option", language, "Please pick one of the options below:")
            await send_buttons(phone, prompt, COLOR_CONFIRM_BUTTONS)
            return

        if state == "awaiting_color_manual":
            text = (msg.text or "").strip()
            if text.lower() == "skip":
                pass
            elif text.startswith("#") and len(text) == 7:
                profile.primary_color = text.upper()
            else:
                bad_color = await i18n.t(
                    "bad_color", language,
                    "That doesn't look like a hex color. Try e.g. #E91E63, or type 'skip'.",
                )
                await send_text(phone, bad_color)
                return

            ask_tone = await i18n.t("ask_tone", language, "Last question — what's your brand vibe?")
            await send_buttons(phone, ask_tone, TONE_BUTTONS)
            biz.onboarding_state = "awaiting_tone"
            return

        if state == "awaiting_tone":
            tone = msg.button_id or (msg.text or "").strip().lower()
            if tone not in ("premium", "friendly", "bold"):
                prompt = await i18n.t("pick_option", language, "Please pick one of the options below:")
                await send_buttons(phone, prompt, TONE_BUTTONS)
                return
            profile.tone = tone
            biz.onboarding_state = "done"

            from app.credits import add_credits
            add_credits(db, business_id, settings.SIGNUP_BONUS_CREDITS, reason="signup_bonus")

            pending_request = biz.pending_first_request
            biz.pending_first_request = None

            # Commit now, not just at the end of this `with` block -- the
            # state transition, cleared pending_first_request, and signup
            # credits must be durable and visible before we hand off to
            # orchestrator.generate() below, which opens its own fresh
            # session and would otherwise see pre-commit (stale) data.
            db.commit()

            if pending_request:
                done_msg = await i18n.t(
                    "onboarding_done_with_request", language,
                    "🎉 You're all set! You have {credits} free credits.\n\n"
                    "Now let's make what you asked for...",
                    credits=settings.SIGNUP_BONUS_CREDITS,
                )
            else:
                done_msg = await i18n.t(
                    "onboarding_done", language,
                    "🎉 You're all set! You have {credits} free credits.\n\n"
                    "Try: *Create a weekend offer post*\n\n"
                    "Anytime, you can also type:\n"
                    "• *credits* — check your balance\n"
                    "• *history* — see recent creatives\n"
                    "• *topup* — buy more credits",
                    credits=settings.SIGNUP_BONUS_CREDITS,
                )
            await send_text(phone, done_msg)

            if pending_request:
                from app.engine.orchestrator import generate as run_generation
                await run_generation(business_id, IncomingMessage(sender=phone, type="text", text=pending_request))
            return

        logger.warning("Unknown onboarding state '%s' for business=%s", state, business_id)
        biz.onboarding_state = "new"
