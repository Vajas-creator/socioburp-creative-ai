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

IMPORTANT: business/profile are read once inside a `with get_session()`
block and immediately converted to a plain BusinessContext. Every function
below that point (prompt_builder, caption, etc.) takes that plain context —
never a live ORM object — because the session closes before those (slow,
async, external-API-calling) functions run. See app/engine/context.py.
"""
import logging
import uuid

import httpx

from app.db import get_session
from app.models import Business, BrandProfile, Generation, ConversationState
from app.schemas import IncomingMessage
from app.whatsapp.client import send_text, send_image
from app.storage import upload_creative
from app.credits import charge_for_generation, get_balance
from app.config import settings
from app.engine.context import BusinessContext
from app.engine import intent as intent_engine
from app.engine import prompt_builder
from app.engine import image_gen
from app.engine import compositor
from app.engine import caption as caption_engine
from app.engine import quality

logger = logging.getLogger("socioburp.engine.orchestrator")

REGEN_THRESHOLD = 60


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


async def generate(business_id: uuid.UUID, msg: IncomingMessage):
    if not msg.text:
        await send_text(msg.sender, "Please describe what you'd like as a text message 🙂")
        return

    phone = msg.sender

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
        )

        within_limit = _check_rate_limit(db, business_id)

    if not within_limit:
        await send_text(
            phone,
            f"You've hit the limit of {settings.MAX_GENERATIONS_PER_HOUR} creatives per hour 🙏 "
            "Please try again in a bit — this just protects against accidental spam.",
        )
        return

    # --- Classify intent ---
    result = await intent_engine.classify(msg.text)
    user_intent = result["intent"]
    brief = result["brief"]

    if user_intent in ("QUESTION", "OTHER"):
        await send_text(
            phone,
            "I'm your creative assistant! Try something like:\n"
            "• *Create a weekend offer post*\n"
            "• *Make a Diwali sale creative*\n\n"
            "Or type *credits* / *history* / *topup* anytime.",
        )
        return

    is_revision = user_intent == "REVISE" and last_generation_id is not None

    await send_text(phone, "🎨 Creating your design... (~30 seconds)")

    try:
        # --- Build the image prompt ---
        if is_revision:
            with get_session() as db:
                parent = db.query(Generation).filter(Generation.id == last_generation_id).first()
                base_prompt = parent.built_prompt if parent else None

            if base_prompt:
                built = await prompt_builder.build(
                    ctx, f"Revise this existing creative concept: '{base_prompt}'. Requested change: {brief}",
                )
            else:
                built = await prompt_builder.build(ctx, brief)
        else:
            built = await prompt_builder.build(ctx, brief)

        image_prompt = built["image_prompt"]
        notes_for_caption = built["notes_for_caption"]

        # --- Generate candidates ---
        candidates = await image_gen.generate_images(image_prompt, count=2)

        if not candidates:
            await send_text(
                phone,
                "Hmm, the design generation hit a snag 🙏 No credits were charged — please try again.",
            )
            return

        # --- Quality check + one regen if needed ---
        scored = await quality.score_and_pick(candidates)

        if scored["best_score"] < REGEN_THRESHOLD:
            logger.info(
                "Quality below threshold (%s < %s) for business=%s, regenerating once",
                scored["best_score"], REGEN_THRESHOLD, business_id,
            )
            retry_candidates = await image_gen.generate_images(image_prompt, count=2)
            if retry_candidates:
                retry_scored = await quality.score_and_pick(retry_candidates)
                if retry_scored["best_score"] > scored["best_score"]:
                    candidates = retry_candidates
                    scored = retry_scored

        best_image = candidates[scored["best_index"]]

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
                user_message=msg.text,
                built_prompt=image_prompt,
                quality_score=scored["best_score"],
                status="generating",
                parent_id=last_generation_id if is_revision else None,
            )
            db.add(gen_row)
            db.flush()
            generation_id = gen_row.id

        image_url = upload_creative(business_id, generation_id, best_image)
        full_caption = f"{cap['caption']}\n\n{cap['hashtags']}"

        with get_session() as db:
            gen_row = db.query(Generation).filter(Generation.id == generation_id).first()
            gen_row.image_url = image_url
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

        await send_image(phone, image_url, caption=full_caption[:1024])
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
