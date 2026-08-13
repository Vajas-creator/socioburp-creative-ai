"""
Week 2 orchestrator — the main event. Wires together:
  1. Intent detection (GENERATE vs REVISE vs QUESTION vs OTHER)
  2. Prompt building (brief + brand profile -> detailed image prompt)
  3. Image generation (2 candidates)
  4. Logo compositing
  5. Quality scoring + one regen if below threshold
  6. Caption + hashtag generation
  7. Upload to R2, save Generation row, charge 1 credit
  8. Deliver on WhatsApp

Revisions ("make it more premium") reuse the original built_prompt as a
starting point rather than building from scratch, and link back via parent_id.

Revision fast path (Week 3+): before running the full pipeline on a REVISE,
revision_classifier.classify() checks whether the request is ONLY a logo move
("logo top left"). If so, and the parent generation stored its pre-composite
background (base_image_url), we just re-paste the logo at the new position —
no prompt build, no image generation, no quality check, zero credits charged.
If the parent has no base_image_url (older rows, or the base upload failed),
we fall back to the normal full-regeneration revision path.

Concept proposal step (runs BEFORE the pipeline above):
  1. If a proposal is already pending for this business, the incoming message
     is interpreted as a reply to THAT proposal (confirm or adjust) — intent
     classification is skipped entirely, since we already know what's going on.
  2. Otherwise, classify intent as before. QUESTION/OTHER unchanged. REVISE
     unchanged (operates on the last completed generation).
  3. For a fresh GENERATE with no pending proposal: ask concept_proposal.decide()
     whether this needs a proposal first. If yes, send the proposal and stop —
     no image generation, no charge, nothing produced yet. If the request was
     already specific enough, skip straight to generation as before.

pending_proposal is stored as a JSON string on ConversationState containing
both the client-facing proposal_text and the internal concept_brief (used to
actually build the creative once confirmed) — one text column, two values.

IMPORTANT: business/profile are read once inside a `with get_session()`
block and immediately converted to a plain BusinessContext. Every function
below that point (prompt_builder, caption, etc.) takes that plain context —
never a live ORM object — because the session closes before those (slow,
async, external-API-calling) functions run. See app/engine/context.py.
"""
import asyncio
import json
import logging
import uuid

import httpx

from app.db import get_session
from app.models import Business, BrandProfile, Generation, ConversationState
from app.schemas import IncomingMessage
from app.whatsapp.client import send_text, send_image, send_image_with_button, download_media
from app.storage import upload_creative, upload_base_image, upload_carousel_slide, upload_reference_image
from app.credits import charge_for_generation, get_balance, regen_within_budget, record_regen_used
from app.config import settings
from app.engine.context import BusinessContext
from app.engine import intent as intent_engine
from app.engine import revision_classifier
from app.engine import concept_proposal
from app.engine import prompt_builder
from app.engine import image_gen
from app.engine import compositor
from app.engine import caption as caption_engine
from app.engine import quality
from app.engine import learning
from app.engine import industry_research
from app.engine import brand_reflection

logger = logging.getLogger("socioburp.engine.orchestrator")

REGEN_THRESHOLD = 60
CAROUSEL_MIN_SLIDES = 1
CAROUSEL_MAX_SLIDES = 8
CAROUSEL_CREDIT_PER_SLIDE = 1  # same rate as a single creative


def _check_rate_limit(db, business_id: uuid.UUID) -> bool:
    """
    Returns True if the business is within its hourly generation limit,
    False if they've hit MAX_GENERATIONS_PER_HOUR. Checked before any paid
    API calls (Claude, image gen) run — an abuse guard, not a UX feature,
    so a plain count over the last hour is enough; no need for a token
    bucket or sliding window here.
    """
    from datetime import datetime, timedelta, timezone
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    recent_count = (
        db.query(Generation)
        .filter(Generation.business_id == business_id, Generation.created_at >= one_hour_ago)
        .count()
    )
    return recent_count < settings.MAX_GENERATIONS_PER_HOUR


async def _deliver_creative(phone: str, business_id: uuid.UUID, generation_id: uuid.UUID, image_url: str, caption: str):
    """
    Send the finished creative to WhatsApp. If the business is onboarded for
    Instagram auto-posting (Business.instagram_account_id is set), deliver
    with a "Post to Instagram" reply button instead of a plain image —
    tapping it is handled in router.py -> app/instagram.py.
    """
    with get_session() as db:
        business = db.query(Business).filter(Business.id == business_id).first()
        instagram_account_id = business.instagram_account_id if business else None

    if instagram_account_id:
        await send_image_with_button(
            phone, image_url, body=caption,
            button_id=f"post_ig_{generation_id}", button_label="Post to Instagram",
        )
    else:
        await send_image(phone, image_url, caption=caption)


async def generate(business_id: uuid.UUID, msg: IncomingMessage):
    # An image with NO caption is now intercepted in app/router.py before
    # ever reaching here -- see app/engine/image_intent.py, which asks
    # what to do with it instead of this generic bail-out. This still
    # applies to a genuinely empty/unsupported text message.
    if not msg.text:
        await send_text(msg.sender, "Please describe what you'd like as a text message 🙂")
        return

    phone = msg.sender

    # A photo sent WITH instructions (e.g. "change the background to black,
    # add a 25% off overlay") -- download it now so it can be passed to
    # image_gen as an edit reference instead of being generated from a text
    # description alone. Only wired into the single-turn paths below
    # (SPECIFIC_ENOUGH, REVISE) where the client's message is used as-is;
    # a NEEDS_PROPOSAL negotiation spans multiple messages and there's
    # nowhere yet to persist image bytes across those turns, so a photo
    # attached to a request that needs a proposal first falls back to the
    # existing text-only flow for that turn.
    reference_image = None
    if msg.type == "image" and msg.media_id:
        try:
            reference_image = await download_media(msg.media_id)
        except Exception:
            logger.exception("Reference image download failed for business=%s — generating from text alone", business_id)

    # --- Load business context (ORM objects never leave this block) ---
    with get_session() as db:
        business = db.query(Business).filter(Business.id == business_id).first()
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == business_id).first()
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        if convo is None:
            convo = ConversationState(business_id=business_id)
            db.add(convo)
            db.flush()
        last_generation_id = convo.last_generation_id
        pending_raw = convo.pending_proposal

        ctx = BusinessContext(
            name=business.name,
            industry=business.industry,
            tone=profile.tone if profile else None,
            primary_color=profile.primary_color if profile else None,
            secondary_color=profile.secondary_color if profile else None,
            target_audience=profile.target_audience if profile else None,
            website=profile.website if profile else None,
            contact_phone=profile.contact_phone if profile else None,
            logo_url=profile.logo_url if profile else None,
            learned_preferences=list((profile.extras or {}).get("learned_preferences", [])) if profile else [],
            style_summary=(profile.extras or {}).get("style_summary") if profile else None,
            language=business.preferred_language or "en",
            industry_style=industry_research.get_cached_style(business.industry),
            instagram_handle=business.instagram_handle,
            instagram_bio=profile.instagram_bio if profile else None,
            instagram_recent_captions=profile.instagram_recent_captions if profile else None,
        )

        within_limit = _check_rate_limit(db, business_id)

    if not within_limit:
        await send_text(
            phone,
            f"You've hit the limit of {settings.MAX_GENERATIONS_PER_HOUR} creatives per hour 🙏 "
            "Please try again in a bit — this just protects against accidental spam.",
        )
        return

    # --- Branch 1: a proposal is already pending — interpret this reply ---
    if pending_raw:
        pending = json.loads(pending_raw)
        result = await concept_proposal.interpret_reply(ctx, pending["proposal_text"], msg.text)

        # A photo attached to THIS reply takes priority over one stored
        # from an earlier turn in the same negotiation; persist a fresh
        # one immediately (R2, not just the WhatsApp media_id) so it
        # survives however many more ADJUST rounds happen before this
        # actually generates -- previously a photo attached to a request
        # vague enough to need a proposal was simply dropped and never
        # revisited, even once the client confirmed. See the Aug 2026
        # "uploaded reference images are being ignored" investigation.
        reference_image_url = pending.get("reference_image_url")
        if reference_image is not None:
            try:
                reference_image_url = await asyncio.to_thread(upload_reference_image, business_id, reference_image)
            except Exception:
                logger.exception("Failed to persist reference photo during proposal negotiation for business=%s", business_id)

        async def _resolve_reference_bytes():
            """Prefer bytes already downloaded on THIS message; otherwise fetch whatever's stored from an earlier turn."""
            if reference_image is not None:
                return reference_image
            if not reference_image_url:
                return None
            try:
                async with httpx.AsyncClient(timeout=15.0) as http_client:
                    resp = await http_client.get(reference_image_url)
                if resp.status_code == 200:
                    return resp.content
                logger.warning("Fetching stored proposal reference image returned %s", resp.status_code)
            except Exception:
                logger.exception("Failed to fetch stored proposal reference image for business=%s", business_id)
            return None

        if result["classification"] == "RETRY":
            # interpret_reply() itself failed (API hiccup) — ask again rather
            # than guessing. pending_proposal is deliberately left untouched
            # so the client's next reply gets a fresh, independent attempt.
            await send_text(
                phone,
                "Sorry, I didn't quite catch that 🙏 Want me to go ahead with what "
                "I proposed above, or is there something you'd like to change?",
            )
            return  # no generation yet, no charge

        if result["classification"] == "ADJUST":
            current_adjust_count = pending.get("adjust_count", 0)
            new_adjust_count = current_adjust_count + 1

            if new_adjust_count >= 3:
                # 2 rounds of pre-generation back-and-forth is enough —
                # beyond that, generate with what's been gathered and let
                # the client revise the actual image instead of continuing
                # to negotiate over text with nothing visual to react to.
                with get_session() as db:
                    convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
                    convo.pending_proposal = None
                if last_generation_id:
                    await learning.record_accepted_direction(business_id, last_generation_id)
                await send_text(phone, "Let's create this now — you can adjust anything directly on the image after 🎨")
                await _run_generation(
                    business_id, phone, ctx, result["concept_brief"], msg.text, last_generation_id,
                    is_revision=False, trigger_source="adjust_cap", reference_image=await _resolve_reference_bytes(),
                )
                return

            new_pending = {
                "proposal_text": result["proposal_text"],
                "concept_brief": result["concept_brief"],
                "adjust_count": new_adjust_count,
                "reference_image_url": reference_image_url,
            }
            with get_session() as db:
                convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
                convo.pending_proposal = json.dumps(new_pending)
            await send_text(phone, result["proposal_text"])
            return  # no generation yet, no charge — still discussing

        # CONFIRM — proceed to generation using the previously agreed concept
        brief = pending["concept_brief"]
        with get_session() as db:
            convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
            convo.pending_proposal = None
        if last_generation_id:
            # Client is moving on to something new without having revised
            # the prior generation first — treat it as accepted (subject to
            # the quality gate inside record_accepted_direction).
            await learning.record_accepted_direction(business_id, last_generation_id)
        await _run_generation(
            business_id, phone, ctx, brief, msg.text, last_generation_id,
            is_revision=False, trigger_source="proposal_confirmed", reference_image=await _resolve_reference_bytes(),
        )
        return

    # --- Branch 2: no pending proposal — classify intent as before ---
    result = await intent_engine.classify(msg.text)
    user_intent = result["intent"]
    brief = result["brief"]

    if user_intent in ("QUESTION", "OTHER"):
        await send_text(
            phone,
            "I'm Sakshi, your creative partner here! Try something like:\n"
            "• *Create a weekend offer post*\n"
            "• *Make a Diwali sale creative*\n\n"
            "Or type *credits* / *history* / *topup* anytime.",
        )
        return

    is_revision = user_intent == "REVISE" and last_generation_id is not None

    if is_revision:
        # Classify the revision first: a pure "move my logo" request is served
        # from the parent's stored pre-composite background for free, skipping
        # the whole paid pipeline. Anything else (or anything we can't serve
        # from the stored base) runs the full revision pipeline as before.
        rev = await revision_classifier.classify(msg.text)

        if rev["revision_type"] == "LOGO_POSITION":
            handled = await _recomposite_logo(
                business_id, phone, ctx, rev["position"], msg.text, last_generation_id,
            )
            if handled:
                return
            logger.info(
                "Logo-position fast path unavailable for business=%s (no base image or no logo) "
                "— falling back to full regeneration", business_id,
            )

        # Use the client's own words, not rev["brief"] -- that's a one-line
        # paraphrase of a single message and is exactly where specific
        # instructions ("change the background to black") get lost. There's
        # no multi-turn negotiation to aggregate here, so the raw message is
        # strictly more faithful than a re-summarized version of itself.
        await _run_generation(business_id, phone, ctx, msg.text, msg.text, last_generation_id, is_revision=True, trigger_source="revision", reference_image=reference_image)
        return

    # --- Branch 3: fresh GENERATE request — decide whether to propose first ---
    decision = await concept_proposal.decide(ctx, brief)

    if decision["decision"] == "NEEDS_PROPOSAL":
        # Persist any attached photo now (R2, not just the WhatsApp
        # media_id) so it survives the negotiation ahead instead of being
        # silently dropped -- see Branch 1 above, which resolves this same
        # field on every subsequent turn. See the Aug 2026 "uploaded
        # reference images are being ignored" investigation.
        reference_image_url = None
        if reference_image is not None:
            try:
                reference_image_url = await asyncio.to_thread(upload_reference_image, business_id, reference_image)
            except Exception:
                logger.exception("Failed to persist reference photo starting a proposal negotiation for business=%s", business_id)

        pending = {
            "proposal_text": decision["proposal_text"], "concept_brief": decision["concept_brief"],
            "adjust_count": 0, "reference_image_url": reference_image_url,
        }
        with get_session() as db:
            convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
            convo.pending_proposal = json.dumps(pending)
        await send_text(phone, decision["proposal_text"])
        return  # no generation yet, no charge — proposing a direction first

    # SPECIFIC_ENOUGH — skip the proposal, go straight to generation.
    # Use the client's own words, not decision["brief"] -- by this point
    # the message has already been paraphrased once by intent_engine.classify()
    # and decision["brief"] is a Claude call re-summarizing THAT paraphrase,
    # not the original. Two lossy hops in a row is exactly how specific
    # details ("25% off overlay") go missing; there's no proposal
    # negotiation to aggregate in this branch, so the raw message loses
    # nothing by being used directly.
    if last_generation_id:
        await learning.record_accepted_direction(business_id, last_generation_id)
    await _run_generation(business_id, phone, ctx, msg.text, msg.text, last_generation_id, is_revision=False, trigger_source="specific_enough", reference_image=reference_image)


async def generate_carousel(
    business_id: uuid.UUID, phone: str, ctx: BusinessContext,
    slide_briefs: list[str], user_message: str, reference_image_url: str | None = None,
):
    """
    Generates len(slide_briefs) genuinely SEPARATE images, one per brief,
    and posts them together as a native Instagram carousel (see
    app/instagram.py's content_type="carousel" branch -- the Make.com
    scenario already supported this; nothing on the app side ever produced
    a request shaped for it before, and the prior version of this function
    generated a fixed 3 slides with no per-slide content, which is what
    this replaces). If exactly one brief is given, delivers/posts as a
    normal single photo instead -- Instagram's carousel API requires at
    least 2 media items, see app/engine/carousel.py where "1" is offered
    as a valid choice anyway (routes here the same way, just posts as a
    photo).

    Called only after app/engine/carousel.py's negotiation (how many
    slides, what each one should show) has completed -- this function
    itself does no asking, just generates from what it's given. Charges
    CAROUSEL_CREDIT_PER_SLIDE credits per slide; app/engine/carousel.py
    checks the balance before calling this.

    reference_image_url: an uploaded photo (already persisted to R2 by
    app/engine/carousel.py, since WhatsApp media IDs aren't guaranteed to
    stay resolvable across a multi-turn negotiation) to use as the actual
    subject for EVERY slide via the image-edit endpoint, if the client
    attached one -- fetched once here and reused across all slides, so
    "make a carousel of this product" genuinely features that product
    throughout instead of generating unrelated new images that merely
    match the text description.

    Deliberately does NOT update ConversationState.last_generation_id --
    the single-image revision pipeline (parent.built_prompt,
    parent.base_image_url) isn't shaped for a multi-slide parent, so a
    follow-up "make it more premium" should keep targeting the last
    single-image generation, not this carousel.
    """
    slide_count = len(slide_briefs)
    credit_cost = slide_count * CAROUSEL_CREDIT_PER_SLIDE

    reference_bytes = None
    if reference_image_url:
        try:
            async with httpx.AsyncClient(timeout=15.0) as http_client:
                ref_resp = await http_client.get(reference_image_url)
            if ref_resp.status_code == 200:
                reference_bytes = ref_resp.content
            else:
                logger.warning(
                    "Fetching carousel reference image returned %s — generating from text alone",
                    ref_resp.status_code,
                )
        except Exception:
            logger.exception("Failed to fetch carousel reference image — generating from text alone")

    with get_session() as db:
        within_limit = _check_rate_limit(db, business_id)

    if not within_limit:
        await send_text(
            phone,
            f"You've hit the limit of {settings.MAX_GENERATIONS_PER_HOUR} creatives per hour 🙏 "
            "Please try again in a bit — this just protects against accidental spam.",
        )
        return

    await send_text(
        phone,
        f"🎠 Creating your {slide_count}-slide carousel... (~a minute)"
        if slide_count > 1 else "🎨 Creating your design... (~30 seconds)",
    )

    try:
        slide_images = []
        for i, slide_brief in enumerate(slide_briefs, start=1):
            built = await prompt_builder.build(
                ctx,
                (
                    f"This is slide {i} of {slide_count} in an Instagram carousel post -- "
                    f"this specific slide's subject/content is: {slide_brief}. "
                    f"Keep a consistent visual style and color palette with the rest of the "
                    f"carousel, but this slide shows ONLY: {slide_brief}."
                    if slide_count > 1 else slide_brief
                ),
            )
            candidates = await image_gen.generate_images(
                built["image_prompt"], count=1, reference_image=reference_bytes,
            )
            if not candidates:
                raise RuntimeError(f"No image returned for carousel slide {i}")

            slide_image = candidates[0]
            if ctx.has_logo:
                async with httpx.AsyncClient(timeout=15.0) as http_client:
                    logo_resp = await http_client.get(ctx.logo_url)
                    if logo_resp.status_code == 200:
                        slide_image = compositor.composite_logo(slide_image, logo_resp.content)

            slide_images.append(slide_image)

        cap = await caption_engine.generate(ctx, user_message)

        with get_session() as db:
            gen_row = Generation(
                business_id=business_id,
                user_message=user_message,
                built_prompt=f"[carousel, {slide_count} slides: {' | '.join(slide_briefs)}]",
                status="generating",
                credits_charged=credit_cost,
                trigger_source="carousel",
            )
            db.add(gen_row)
            db.flush()
            generation_id = gen_row.id

        # Offloaded to threads -- same reasoning as the single-image path,
        # see upload_creative's call sites above.
        image_urls = [
            await asyncio.to_thread(upload_carousel_slide, business_id, generation_id, i + 1, img)
            for i, img in enumerate(slide_images)
        ]

        full_caption = f"{cap['caption']}\n\n{cap['hashtags']}"

        with get_session() as db:
            gen_row = db.query(Generation).filter(Generation.id == generation_id).first()
            gen_row.image_url = image_urls[0]  # first slide doubles as the WhatsApp preview thumbnail
            # Instagram's carousel API needs >=2 media items -- a single
            # slide posts as a plain photo instead (see app/instagram.py's
            # branch, keyed off this being None vs populated).
            gen_row.carousel_image_urls = image_urls if slide_count >= 2 else None
            gen_row.caption = cap["caption"]
            gen_row.hashtags = cap["hashtags"]
            gen_row.status = "done"

        charge_for_generation(business_id, generation_id, amount=credit_cost)

        balance = get_balance(business_id)
        low_balance_note = (
            f"\n\n⚠️ Only {balance} credits left. Reply *topup* to recharge."
            if balance <= settings.LOW_BALANCE_THRESHOLD else ""
        )

        # All but the last slide as plain images; the last one carries the
        # "Post to Instagram" button + caption, same delivery shape as a
        # single creative (see _deliver_creative) -- for a 1-slide
        # "carousel" this loop is empty and only the single button-image
        # send below runs, identical to a normal single-image delivery.
        for url in image_urls[:-1]:
            await send_image(phone, url)

        with get_session() as db:
            biz_row = db.query(Business).filter(Business.id == business_id).first()
            instagram_account_id = biz_row.instagram_account_id if biz_row else None

        if instagram_account_id:
            await send_image_with_button(
                phone, image_urls[-1], body=full_caption[:1024],
                button_id=f"post_ig_{generation_id}", button_label="Post to Instagram",
            )
        else:
            await send_image(phone, image_urls[-1], caption=full_caption[:1024])

        await send_text(
            phone,
            (f"✨ Here's your {slide_count}-slide carousel!\n\n" if slide_count > 1 else "✨ Here's your creative!\n\n")
            + f"💳 Credits left: {balance}{low_balance_note}",
        )

    except Exception:
        logger.exception("Carousel generation failed for business=%s", business_id)
        await send_text(
            phone,
            "Something went wrong creating your carousel 🙏 No credits were charged. Please try again.",
        )


async def _recomposite_logo(business_id, phone, ctx, position, user_message, parent_id):
    """
    Free logo-move revision: re-paste the logo onto the parent generation's
    stored pre-composite background at the requested position. No prompt
    build, no image generation, no quality check, no charge.

    Returns True if handled. Returns False when the fast path can't run —
    parent missing, no stored base image, no logo on file, or a fetch
    failure — so the caller falls back to the full pipeline.
    """
    with get_session() as db:
        parent = db.query(Generation).filter(Generation.id == parent_id).first()
        if parent is None or not parent.base_image_url:
            return False
        base_image_url = parent.base_image_url
        parent_prompt = parent.built_prompt
        parent_caption = parent.caption
        parent_hashtags = parent.hashtags
        parent_score = parent.quality_score

    if not ctx.has_logo:
        return False

    try:
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            base_resp = await http_client.get(base_image_url)
            logo_resp = await http_client.get(ctx.logo_url)
        if base_resp.status_code != 200 or logo_resp.status_code != 200:
            return False
    except Exception:
        logger.exception("Fetching base image/logo failed for business=%s — falling back", business_id)
        return False

    composited = compositor.composite_logo(base_resp.content, logo_resp.content, position=position)

    try:
        with get_session() as db:
            gen_row = Generation(
                business_id=business_id,
                user_message=user_message,
                built_prompt=parent_prompt,
                quality_score=parent_score,
                credits_charged=0,
                status="generating",
                parent_id=parent_id,
                base_image_url=base_image_url,  # carry forward so the logo can be moved again
                trigger_source="logo_free_revision",
            )
            db.add(gen_row)
            db.flush()
            generation_id = gen_row.id

        # Offloaded to a thread -- upload_creative() is a synchronous,
        # blocking boto3 call; called directly it would block the entire
        # asyncio event loop (every user's request, not just this one) for
        # as long as R2 takes to respond. See the Aug 2026 "bot goes
        # silent" incident.
        image_url = await asyncio.to_thread(upload_creative, business_id, generation_id, composited)

        with get_session() as db:
            gen_row = db.query(Generation).filter(Generation.id == generation_id).first()
            gen_row.image_url = image_url
            gen_row.caption = parent_caption
            gen_row.hashtags = parent_hashtags
            gen_row.status = "done"

            convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
            convo.last_generation_id = generation_id

        # Deliberately NO charge_for_generation here — logo moves are free.
        full_caption = f"{parent_caption}\n\n{parent_hashtags}" if parent_caption else ""
        await _deliver_creative(phone, business_id, generation_id, image_url, full_caption[:1024])
        await send_text(
            phone,
            f"✅ Moved your logo to the {position.replace('-', ' ')} — no credit charged for logo moves!\n\n"
            f"💳 Credits left: {get_balance(business_id)}",
        )
        return True

    except Exception:
        logger.exception("Logo recomposite failed for business=%s", business_id)
        await send_text(
            phone,
            "Something went wrong moving your logo 🙏 No credits were charged. Please try again.",
        )
        return True  # handled (with an error message) — don't run the paid pipeline on top


async def _run_generation(business_id, phone, ctx, brief, user_message, last_generation_id, is_revision, trigger_source, reference_image: bytes | None = None):
    """
    The actual production pipeline (Week 2, unchanged): prompt build -> image
    gen -> quality check -> composite -> caption -> save -> charge -> deliver.

    trigger_source records how this generation was initiated — see
    Generation.trigger_source in app/models.py for the full value set.
    Used by the weekly instrumentation query to measure how often the
    ADJUST-round cap actually fires.

    reference_image: raw bytes of a photo the client attached to this
    request (e.g. a product photo), if any — passed through to image_gen
    as an edit reference instead of generating from a text description
    alone. None for text-only requests, or when the caller couldn't
    download it (see generate()).
    """
    try:
        if last_generation_id is None:
            # This business's very first-ever generation -- a one-time,
            # persona-voiced "here's what I've noticed" moment instead of the
            # plain "Creating your design..." status line every generation
            # after this one gets. See app/engine/brand_reflection.py.
            # Moved inside the try (was previously a bare call before it) --
            # a failure here (e.g. a WhatsApp send hiccup) must land in the
            # same error handling as the rest of the pipeline, not escape
            # uncaught. See the Aug 2026 "bot goes silent after 'give me a
            # moment'" incident.
            first_result_msg = await brand_reflection.reflect_first_result(ctx)
            await send_text(phone, first_result_msg)
        else:
            await send_text(phone, "🎨 Creating your design... (~30 seconds)")

        # --- Build the image prompt ---
        if is_revision:
            with get_session() as db:
                parent = db.query(Generation).filter(Generation.id == last_generation_id).first()
                base_prompt = parent.built_prompt if parent else None
                parent_image_url = (parent.base_image_url or parent.image_url) if parent else None

            if base_prompt:
                built = await prompt_builder.build(
                    ctx, f"Revise this existing creative concept: '{base_prompt}'. Requested change: {brief}",
                )
            else:
                built = await prompt_builder.build(ctx, brief)

            # Fetch the parent's actual image to use as an edit reference,
            # if this message didn't already attach one (e.g. the client
            # re-attached a fresh photo instead of asking to revise the
            # prior result -- that reference_image, if present, takes
            # priority). This is what makes "change background to black"
            # actually edit the specific image the client is looking at,
            # instead of silently regenerating an unrelated new one from a
            # re-described text prompt -- see the Aug 2026 "bot doesn't
            # know which image I mean" incident. base_image_url (the
            # pre-logo-composite background) is preferred over image_url
            # (the final, logo-composited creative) so the downstream
            # logo-compositing step below still applies cleanly to the
            # edited result.
            if reference_image is None and parent_image_url:
                try:
                    async with httpx.AsyncClient(timeout=15.0) as http_client:
                        parent_resp = await http_client.get(parent_image_url)
                    if parent_resp.status_code == 200:
                        reference_image = parent_resp.content
                    else:
                        logger.warning(
                            "Fetching parent image for revision reference returned %s — falling back to text-only revision, generation=%s",
                            parent_resp.status_code, last_generation_id,
                        )
                except Exception:
                    logger.exception(
                        "Failed to fetch parent image for revision reference — falling back to text-only revision, generation=%s",
                        last_generation_id,
                    )
        else:
            built = await prompt_builder.build(ctx, brief)

        image_prompt = built["image_prompt"]
        notes_for_caption = built["notes_for_caption"]

        # --- Generate candidates ---
        candidates = await image_gen.generate_images(image_prompt, count=2, reference_image=reference_image)

        if not candidates:
            await send_text(
                phone,
                "Hmm, the design generation hit a snag 🙏 No credits were charged — please try again.",
            )
            return

        # --- Quality check + one regen if needed (budget-gated) ---
        scored = await quality.score_and_pick(candidates)

        if scored["best_score"] < REGEN_THRESHOLD:
            if not regen_within_budget(business_id):
                # This business has used its earned quality-check regen
                # allowance for the current credit batch (see credits.py).
                # Per policy: block rather than deliver a known-low-quality
                # result — no charge, nothing delivered. Saved as a
                # 'blocked' Generation row (0 credits) for visibility.
                logger.info(
                    "Regen allowance exhausted for business=%s (score=%s) — blocking delivery",
                    business_id, scored["best_score"],
                )
                with get_session() as db:
                    db.add(Generation(
                        business_id=business_id,
                        user_message=user_message,
                        built_prompt=image_prompt,
                        quality_score=scored["best_score"],
                        credits_charged=0,
                        status="blocked",
                        parent_id=last_generation_id if is_revision else None,
                        trigger_source=trigger_source,
                    ))
                await send_text(
                    phone,
                    "This one didn't quite meet our quality bar 🙏 You've used up the "
                    "regeneration budget for your current credits, so please reach out to "
                    "your SocioBurp contact, or this refreshes automatically once you top "
                    "up. No credits were charged.",
                )
                return

            logger.info(
                "Quality below threshold (%s < %s) for business=%s, regenerating once",
                scored["best_score"], REGEN_THRESHOLD, business_id,
            )
            record_regen_used(business_id)
            retry_candidates = await image_gen.generate_images(image_prompt, count=2, reference_image=reference_image)
            if retry_candidates:
                retry_scored = await quality.score_and_pick(retry_candidates)
                if retry_scored["best_score"] > scored["best_score"]:
                    candidates = retry_candidates
                    scored = retry_scored

        best_image = candidates[scored["best_index"]]
        base_image = best_image  # pre-composite background, kept for free logo-move revisions

        # --- Composite logo if present ---
        if ctx.has_logo:
            async with httpx.AsyncClient(timeout=15.0) as http_client:
                logo_resp = await http_client.get(ctx.logo_url)
                if logo_resp.status_code == 200:
                    best_image = compositor.composite_logo(best_image, logo_resp.content)

        # --- Caption + hashtags ---
        cap = await caption_engine.generate(ctx, notes_for_caption)

        # --- Save generation row + upload ---
        with get_session() as db:
            gen_row = Generation(
                business_id=business_id,
                user_message=user_message,
                built_prompt=image_prompt,
                quality_score=scored["best_score"],
                status="generating",
                parent_id=last_generation_id if is_revision else None,
                trigger_source=trigger_source,
            )
            db.add(gen_row)
            db.flush()
            generation_id = gen_row.id

        # Offloaded to a thread -- see the note on the same pattern in
        # _recomposite_logo() above.
        image_url = await asyncio.to_thread(upload_creative, business_id, generation_id, best_image)
        full_caption = f"{cap['caption']}\n\n{cap['hashtags']}"

        # Also upload the pre-composite background — this is what a future
        # "move my logo" revision re-pastes onto. Best-effort: if it fails,
        # the generation still succeeds and revisions just fall back to full
        # regeneration.
        base_image_url = None
        try:
            base_image_url = await asyncio.to_thread(upload_base_image, business_id, generation_id, base_image)
        except Exception:
            logger.exception("Base image upload failed for generation=%s — logo moves will regenerate", generation_id)

        with get_session() as db:
            gen_row = db.query(Generation).filter(Generation.id == generation_id).first()
            gen_row.image_url = image_url
            gen_row.base_image_url = base_image_url
            gen_row.caption = cap["caption"]
            gen_row.hashtags = cap["hashtags"]
            gen_row.status = "done"

            convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
            convo.last_generation_id = generation_id

        # --- Charge AFTER success, never before ---
        charge_for_generation(business_id, generation_id, amount=1)

        # --- Deliver ---
        balance = get_balance(business_id)
        low_balance_note = (
            f"\n\n⚠️ Only {balance} credits left. Reply *topup* to recharge."
            if balance <= settings.LOW_BALANCE_THRESHOLD else ""
        )

        await _deliver_creative(phone, business_id, generation_id, image_url, full_caption[:1024])
        await send_text(
            phone,
            "✨ Here's your creative!\n\n"
            "Reply to adjust:\n"
            "• \"make it more premium\"\n"
            "• \"change the headline\"\n"
            "• \"brighter colors\"\n\n"
            f"💳 Credits left: {balance}{low_balance_note}",
        )

    except Exception:
        logger.exception("Generation failed for business=%s", business_id)
        await send_text(
            phone,
            "Something went wrong creating your design 🙏 No credits were charged. Please try again.",
        )
