"""
Onboarding state machine. Persisted on the Business row so a client can go
silent mid-flow and resume later without losing progress.

States: new -> awaiting_owner_name -> awaiting_business_description ->
awaiting_instagram -> done

awaiting_owner_name (added Aug 2026): asks the client's own name, once,
right after the welcome message -- purely for personalization (see
app/router.py's bare-greeting reply, which addresses a returning client
by Business.owner_name in preference to Business.name, the business
name). Optional, like the Instagram question: a skip/decline or empty
reply never blocks onboarding completion, it just means personalized
greetings fall back to the business name (or the old generic line if
that's unset too).

Redesigned Aug 2026: cut from a 5-question flow (name, industry, logo,
colors, tone) down to 2 open-ended questions -- what does your business
do, and what's your Instagram -- staged one at a time, short messages
only, no feature menu upfront. business_type/brand_adjectives/business_name
are all extracted from the client's own free-text answer via Claude (see
app/engine/brand_reflection.py), not fixed button categories --
Business.industry is now free text, not a restaurant/salon/other enum.

Logo/manual-color/tone collection (previously mandatory questions) is no
longer part of onboarding. If the client uploads an Instagram screenshot
when asked for their page, real brand colors ARE extracted from it (same
underlying capability as the old color-discovery flow, see
app/engine/color_discovery.py) -- but WITHOUT a separate confirm step this
time, a deliberate break from that module's original "never silently
apply" principle, made specifically for this lower-friction flow. Logo
upload and brand-tone selection have no equivalent question here at all;
BrandProfile.logo_url and .tone simply stay unset for onboarding-only
clients -- every consumer already handles that gracefully (has_logo is
False, tone is optional context everywhere it's read), nothing breaks.

Industry research (background trend caching, see
app/engine/industry_research.py) still fires after business_type is
extracted, keyed on that free-text string -- cache hit rate is lower now
than with 3 fixed categories, but each business still gets a background
enrichment pass with no user-facing wait.

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
from app.whatsapp.client import send_text, download_media
from app.config import settings
from app import i18n
from app.engine import industry_research, color_discovery
from app.engine import intent as intent_engine
from app.engine import brand_reflection

logger = logging.getLogger("socioburp.onboarding")

LANGUAGE_OVERRIDE_KEYWORDS = {
    "english": "en", "hindi": "hi", "hinglish": "hinglish",
    "tamil": "ta", "telugu": "te", "kannada": "kn", "malayalam": "ml",
}

INSTAGRAM_SKIP_WORDS = ("skip", "no", "none", "don't have one", "dont have one", "n/a", "na")
NAME_SKIP_WORDS = ("skip", "no", "none", "n/a", "na", "prefer not to say", "rather not say")

# A short pause between the welcome message and the first real question --
# staged, conversational pacing rather than a wall of text at once. Module
# level so tests can patch it to 0 and skip the real wall-clock delay.
WELCOME_TO_QUESTION_DELAY_SECONDS = 1.5


async def advance(business_id: uuid.UUID, msg: IncomingMessage):
    """
    Returns None in the normal case. When onboarding just completed,
    returns (ctx, brief) instead -- the caller (app/router.py) must pass
    that to orchestrator._run_generation() itself, AFTER this function has
    returned. Deliberately not run from in here: the whole generation
    pipeline (several Claude calls, image gen, R2 uploads, its own several
    DB sessions) is slow, and calling it from inside this function's own
    `with get_session()` block would mean that session/connection stays
    checked out, idle, for the pipeline's entire duration, while the
    pipeline itself needs to check out MORE connections from the same
    pool for its own work -- a real contention/hang risk under any real
    concurrent load. See the Aug 2026 "bot goes silent after 'give me a
    moment'" incident.
    """
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
            # just a greeting ("hi"), the request is remembered
            # (Business.pending_first_request) and auto-generated the
            # moment onboarding finishes, so the client never has to repeat
            # themselves. See the "awaiting_instagram" branch below.
            if msg.text and msg.text.strip():
                intent_result = await intent_engine.classify(msg.text)
                if intent_result["intent"] == "GENERATE":
                    biz.pending_first_request = msg.text.strip()

            welcome = await i18n.t(
                "welcome", language,
                "Hi, I'm Sakshi. 👋\n"
                "I'll help keep your business visible online — without you having to "
                "figure out things on your own daily.\n"
                "First, I'm going to learn a little about your business and how you "
                "like it to look.\n"
                "You can always just message me like you would a person.",
            )
            if language != "en":
                note = await i18n.t(
                    "language_note", language,
                    "(Replying in {language_name} — type 'english' anytime to switch.)",
                    language_name=i18n.LANGUAGE_NAMES[language],
                )
                welcome = f"{welcome}\n\n{note}"
            await send_text(phone, welcome)

            await asyncio.sleep(WELCOME_TO_QUESTION_DELAY_SECONDS)

            ask_name = await i18n.t(
                "ask_owner_name", language,
                "First, what's your name?",
            )
            await send_text(phone, ask_name)
            biz.onboarding_state = "awaiting_owner_name"
            return

        if state == "awaiting_owner_name":
            if msg.text and msg.text.strip() and text_lower not in NAME_SKIP_WORDS:
                biz.owner_name = msg.text.strip()[:100]
            # Optional, same as the Instagram question -- a skip/decline
            # or empty reply never blocks onboarding, personalized
            # greetings just fall back to the business name instead.

            ask_business = await i18n.t(
                "ask_business_description", language,
                "Let's start simple. What does your business do?",
            )
            await send_text(phone, ask_business)
            biz.onboarding_state = "awaiting_business_description"
            return

        if state == "awaiting_business_description":
            if not msg.text or not msg.text.strip():
                retry = await i18n.t(
                    "business_description_needs_text", language,
                    "Just tell me a bit about your business as text 🙂",
                )
                await send_text(phone, retry)
                return

            description = msg.text.strip()
            understood = await brand_reflection.understand_business(description, language)

            biz.industry = understood["business_type"]
            if understood["business_name"]:
                biz.name = understood["business_name"]

            # Fire-and-forget -- runs concurrently with the rest of
            # onboarding, never blocks this reply. No-ops internally if
            # already cached for this exact business_type string.
            asyncio.create_task(industry_research.research_and_cache_if_needed(understood["business_type"]))

            await send_text(phone, understood["message"])

            ask_instagram = await i18n.t(
                "ask_instagram", language,
                "Send me your Instagram page here. I'll study how your brand "
                "currently looks before I create anything.",
            )
            await send_text(phone, ask_instagram)
            biz.onboarding_state = "awaiting_instagram"
            return

        if state == "awaiting_instagram":
            if msg.type == "image" and msg.media_id:
                # A real screenshot -- extract real brand colors from it,
                # same underlying capability as the old color-discovery
                # flow, applied directly. No separate confirm step here:
                # the whole point of this shorter flow is fewer questions,
                # a deliberate break from color_discovery.py's original
                # "never silently apply" caution (see module docstring).
                try:
                    image_bytes = await download_media(msg.media_id)
                    extracted = await color_discovery.extract_colors_from_image(image_bytes)
                    if extracted and extracted.get("confident") and extracted.get("primary_color"):
                        profile.primary_color = extracted["primary_color"]
                        profile.secondary_color = extracted.get("secondary_color")
                except Exception:
                    logger.exception("Instagram screenshot processing failed for business=%s", business_id)
            elif text_lower not in INSTAGRAM_SKIP_WORDS and msg.text and msg.text.strip():
                biz.instagram_handle = msg.text.strip()
                # Fire-and-forget, same pattern as industry_research below --
                # fetches the actual bio + recent captions in the background
                # via Make's Business Discovery scenario and writes them onto
                # BrandProfile once done. Never awaited here: this must not
                # add latency to the "give me a moment" -> first generation
                # path below. The first-ever generation won't have this yet
                # (same as industry research on a cache miss) -- it's there
                # for every generation after that. See
                # app/engine/instagram_analysis.py.
                from app.engine import instagram_analysis
                asyncio.create_task(
                    instagram_analysis.fetch_and_store_profile_summary(business_id, biz.instagram_handle)
                )
            # A skip/decline or empty reply: proceed anyway -- unlike the
            # business-description question, this one never blocks
            # onboarding completion.

            biz.onboarding_state = "done"

            from app.credits import add_credits
            add_credits(db, business_id, settings.SIGNUP_BONUS_CREDITS, reason="signup_bonus")

            pending_request = biz.pending_first_request
            biz.pending_first_request = None
            fallback_brief = (
                f"Create an introductory social media post for this {biz.industry} business"
                if biz.industry else "Create an introductory social media post for this business"
            )
            brief = pending_request or fallback_brief

            # Extract into a plain BusinessContext while biz/profile are
            # still attached -- everything downstream runs after this
            # block commits, by which point touching these ORM objects
            # directly would raise DetachedInstanceError.
            from app.engine.context import BusinessContext
            ctx = BusinessContext(
                name=biz.name,
                industry=biz.industry,
                tone=profile.tone,
                primary_color=profile.primary_color,
                secondary_color=profile.secondary_color,
                target_audience=profile.target_audience,
                language=language,
                industry_style=industry_research.get_cached_style(biz.industry),
                instagram_handle=biz.instagram_handle,
                instagram_bio=profile.instagram_bio,
                instagram_recent_captions=profile.instagram_recent_captions,
            )

            # Commit now, not just at the end of this `with` block -- the
            # state transition, cleared pending_first_request, and signup
            # credits must be durable and visible before we hand off to
            # the generation pipeline below, which opens its own fresh
            # session and would otherwise see pre-commit (stale) data.
            db.commit()

            from app import analytics
            analytics.log_event(business_id, "onboarding_completed", industry=biz.industry)

            # Signal to the caller (app/router.py) to call
            # orchestrator._run_generation() directly, bypassing the
            # normal generate() entry point's concept-proposal gate --
            # generate() could decide it needs more detail and ask ANOTHER
            # question instead of generating, and Sakshi just said "Give me
            # a moment", a promise of immediate action. Same bypass the
            # ADJUST-round-cap escape hatch in orchestrator.generate()
            # already uses. last_generation_id is genuinely None here
            # (this business's first-ever generation), which is also what
            # makes reflect_first_result() fire automatically inside
            # _run_generation(). NOT called from here -- see this
            # function's own docstring for why.
            return ctx, brief

        logger.warning("Unknown onboarding state '%s' for business=%s", state, business_id)
        biz.onboarding_state = "new"
