"""
Multi-turn negotiation for an uploaded photo with no accompanying
instruction. Previously such an upload was silently discarded --
app/engine/orchestrator.py's generate() bailed immediately with "Please
describe what you'd like as a text message" without ever acknowledging a
photo had arrived, and never even downloaded it. This asks what to do
with it instead of guessing or dropping it -- see the Aug 2026 "uploaded
reference images are being ignored" investigation.

Aug 2026 follow-up ("interpreting an image shared unprompted"): the
fixed "change background / use as-is / something else" menu below
assumed every unprompted image was a photo of the client's OWN product/
subject meant for editing. That's wrong for a screenshot, a competitor's
post, a menu, a flyer, or similar -- someone sending one of those wants
Sakshi to actually LOOK at it and respond to what it shows, not be asked
whether to "change its background". _understand_image() now looks at the
image first via Claude vision; only a genuine edit-candidate (or
something genuinely ambiguous, which fails safe to the existing menu)
falls through to the button flow below.

State tracked on ConversationState.pending_image_intent (JSON-in-Text,
same pattern as concept_proposal.py's pending_proposal / carousel.py's
pending_carousel).

The photo itself is persisted to R2 immediately (see
app/storage.py's upload_reference_image()) rather than kept only as a
WhatsApp media_id, so it survives however long the client takes to reply.
"""
import asyncio
import base64
import json
import logging
import uuid

import httpx

from app.db import get_session
from app.models import Business, Generation, ConversationState
from app.schemas import IncomingMessage
from app.whatsapp.client import send_text, send_buttons, download_media
from app.storage import upload_reference_image, upload_creative
from app.engine.context import load_business_context
from app.engine import image_gen, compositor, caption as caption_engine, learning, image_history
from app.image_utils import detect_image_media_type
from app.config import settings
from app import credits, payments, allowlist, alerting

logger = logging.getLogger("socioburp.engine.image_intent")

from app.anthropic_client import create_message
from app.json_extract import extract_json_text

BUTTON_CHANGE_BG = "img_change_bg"
BUTTON_USE_AS_IS = "img_use_as_is"
BUTTON_SOMETHING_ELSE = "img_something_else"

UNDERSTAND_IMAGE_SYSTEM_PROMPT = """You are Sakshi, an AI creative and
marketing partner for a small business, looking at a photo/image the
client just sent on WhatsApp with NO caption or instruction attached.

Decide which of these best describes it:
- EDIT_CANDIDATE: a photo of THEIR OWN product, dish, service, storefront,
  or similar subject -- the kind of photo someone sends wanting help
  turning it into a marketing creative (background changed, used as-is,
  etc).
- INFORMATIVE: the image is informative content in its own right, not
  something to edit -- a screenshot (of Instagram, a competitor, a chat),
  a competitor's post or ad, a menu, a price list, a flyer, a chart or
  graph, a document, or similar. Someone sending this almost always wants
  you to actually look at it and respond to what it shows, not offer to
  "edit" it.
- AMBIGUOUS: genuinely unclear which of the above it is.

If INFORMATIVE, also write a short, specific, useful WhatsApp-length reply
(a few short lines, not an essay) responding to what's actually IN the
image -- e.g. for a competitor's post, comment on what stands out about
their offer/style/pricing and how this business could respond; for a
menu/flyer, note what you notice; for a chart/screenshot, describe what
it's showing and why it matters. Ground it in this business's own profile
below where relevant. Stay within your scope as a marketing/creative
partner -- if the image shows something genuinely unrelated to marketing
or this business, say so briefly rather than forcing a marketing angle
onto it.

Business profile:
{profile}

Reply with JSON only, no other text:
{{"category": "EDIT_CANDIDATE"|"INFORMATIVE"|"AMBIGUOUS", "response": "..."}}
("response" only needs real content when category is INFORMATIVE -- empty string otherwise)"""


async def _understand_image(ctx, image_bytes: bytes) -> dict:
    """
    Returns {"category": "EDIT_CANDIDATE"|"INFORMATIVE"|"AMBIGUOUS",
    "response": str|None}. Fails safe to {"category": "AMBIGUOUS",
    "response": None} on any failure -- that routes to the existing
    button-menu flow, so a vision hiccup never blocks acknowledging the
    photo, it just falls back to the pre-existing behavior.
    """
    profile_lines = [f"Business name: {ctx.name or 'Unknown'}", f"Industry: {ctx.industry or 'Unknown'}"]
    if ctx.tone:
        profile_lines.append(f"Brand tone: {ctx.tone}")

    try:
        media_type = detect_image_media_type(image_bytes)
        response = await create_message(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=500,
            system=UNDERSTAND_IMAGE_SYSTEM_PROMPT.format(profile="\n".join(profile_lines)),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(image_bytes).decode("utf-8")}},
                    {"type": "text", "text": "What is this image, and if it's informative content, what should I say about it?"},
                ],
            }],
        )
        text = extract_json_text(response.content[0].text.strip())
        parsed = json.loads(text)
        category = parsed.get("category")
        if category not in ("EDIT_CANDIDATE", "INFORMATIVE", "AMBIGUOUS"):
            raise ValueError(f"Unexpected category: {category!r}")
        return {"category": category, "response": (parsed.get("response") or "").strip() or None}
    except Exception:
        logger.exception("Image understanding failed for business=%r — falling back to the edit-menu flow", ctx.name)
        return {"category": "AMBIGUOUS", "response": None}


async def _persist_photo(business_id: uuid.UUID, msg: IncomingMessage) -> tuple[str | None, bytes | None]:
    """Returns (r2_url, image_bytes) -- both None if there's no image or persisting it failed."""
    if not (msg.type == "image" and msg.media_id):
        return None, None
    try:
        image_bytes = await download_media(msg.media_id)
        url = await asyncio.to_thread(upload_reference_image, business_id, image_bytes)
        return url, image_bytes
    except Exception:
        logger.exception("Failed to persist uploaded photo for business=%s", business_id)
        return None, None


async def _ask_what_to_do(phone: str):
    await send_buttons(
        phone,
        "Got your photo! What would you like me to do with it?",
        [
            (BUTTON_CHANGE_BG, "Change background"),
            (BUTTON_USE_AS_IS, "Use as-is"),
            (BUTTON_SOMETHING_ELSE, "Something else"),
        ],
    )


def _save_pending(business_id: uuid.UUID, pending: dict):
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        if convo is None:
            convo = ConversationState(business_id=business_id)
            db.add(convo)
        convo.pending_image_intent = json.dumps(pending)


def _clear_pending(business_id: uuid.UUID):
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        if convo:
            convo.pending_image_intent = None


async def start(business_id: uuid.UUID, msg: IncomingMessage):
    """Entry point for an image with no caption (see app/router.py)."""
    phone = msg.sender

    reference_image_url, image_bytes = await _persist_photo(business_id, msg)
    if reference_image_url is None:
        await send_text(phone, "Hmm, I couldn't quite process that image 🙏 Could you try sending it again?")
        return

    # Recorded immediately, uploaded photo though it is -- so a LATER
    # reference like "use the second one" can resolve to it even before
    # it's ever been generated from. See app/engine/image_history.py.
    image_history.record_image(business_id, "uploaded", reference_image_url, "a photo the client uploaded")

    # Aug 2026 "look at what was actually uploaded" fix -- see module
    # docstring. Only a genuine edit-candidate (or something the vision
    # call couldn't confidently classify) falls through to the fixed
    # edit-menu; informative content gets a real, grounded response
    # instead of being asked whether to "change its background".
    ctx, _ = await load_business_context(business_id)
    understood = await _understand_image(ctx, image_bytes)

    if understood["category"] == "INFORMATIVE" and understood["response"]:
        await send_text(phone, understood["response"])
        return

    _save_pending(business_id, {"reference_image_url": reference_image_url})
    await _ask_what_to_do(phone)


async def advance(business_id: uuid.UUID, msg: IncomingMessage, pending_raw: str):
    """
    Called for every message while this negotiation is in progress (see
    app/router.py). "cancel" (and any global command or explicit carousel
    request) is intercepted by app/router.py itself, via
    app/engine/router_intent.py's classifier, BEFORE this function is
    ever called -- this function only ever sees a message meant as an
    answer to whatever's pending.
    """
    phone = msg.sender
    pending = json.loads(pending_raw)

    new_reference_url, _new_image_bytes = await _persist_photo(business_id, msg)
    if new_reference_url:
        pending["reference_image_url"] = new_reference_url

    reference_image_url = pending.get("reference_image_url")
    stage = pending.get("stage")

    if stage is None:
        if msg.button_id == BUTTON_USE_AS_IS:
            _clear_pending(business_id)
            await _use_as_is(business_id, phone, reference_image_url)
            return

        if msg.button_id == BUTTON_CHANGE_BG:
            pending["stage"] = "awaiting_background_instruction"
            _save_pending(business_id, pending)
            await send_text(phone, "Sure — what would you like the new background to be?")
            return

        if msg.button_id == BUTTON_SOMETHING_ELSE:
            pending["stage"] = "awaiting_instruction"
            _save_pending(business_id, pending)
            await send_text(phone, "What would you like me to do with it? (e.g. change the background, add an offer, edit something specific)")
            return

        if msg.text and msg.text.strip():
            # They typed an instruction directly instead of tapping a
            # button -- no need to make them repeat themselves through
            # another round-trip.
            _clear_pending(business_id)
            await _generate_from_instruction(business_id, phone, reference_image_url, msg.text.strip())
            return

        _save_pending(business_id, pending)
        await _ask_what_to_do(phone)
        return

    if stage in ("awaiting_background_instruction", "awaiting_instruction"):
        if not msg.text or not msg.text.strip():
            _save_pending(business_id, pending)
            await send_text(phone, "Just tell me as text what you'd like 🙂")
            return

        instruction = msg.text.strip()
        if stage == "awaiting_background_instruction":
            instruction = f"Change the background to: {instruction}"
        _clear_pending(business_id)
        await _generate_from_instruction(business_id, phone, reference_image_url, instruction)
        return

    logger.warning("Unknown pending_image_intent stage %r for business=%s — clearing", stage, business_id)
    _clear_pending(business_id)


async def _generate_from_instruction(business_id: uuid.UUID, phone: str, reference_image_url: str | None, instruction: str):
    if not allowlist.has_unlimited_access(phone) and credits.get_balance(business_id) < 1:
        await payments.send_topup_options(business_id, phone, prefix="You're out of credits! 🙏 ")
        return

    reference_bytes = None
    if reference_image_url:
        try:
            async with httpx.AsyncClient(timeout=15.0) as http_client:
                resp = await http_client.get(reference_image_url)
            if resp.status_code == 200:
                reference_bytes = resp.content
            else:
                logger.warning("Fetching stored reference image returned %s — generating from text alone", resp.status_code)
        except Exception:
            logger.exception("Failed to fetch stored reference image — generating from text alone")

    ctx, last_generation_id = await load_business_context(business_id)

    # "Moving on = tacit acceptance of whatever came before" -- same
    # principle app/engine/orchestrator.py's generate() already applies on
    # its own SPECIFIC_ENOUGH path. Previously missing here entirely, so
    # the client's preference learning silently stopped the moment they
    # used this path instead of plain text requests.
    if last_generation_id:
        await learning.record_accepted_direction(business_id, last_generation_id)

    from app.engine.orchestrator import _run_generation
    await _run_generation(
        business_id, phone, ctx, instruction, instruction,
        last_generation_id=last_generation_id, is_revision=False,
        trigger_source="image_intent", reference_image=reference_bytes,
    )


async def _use_as_is(business_id: uuid.UUID, phone: str, reference_image_url: str | None):
    """
    "Just post this photo" -- no image generation at all, the uploaded
    photo IS the creative. Still fit to the standard target size and
    logo-composited/captioned the same as any other delivered creative.
    """
    if reference_image_url is None:
        await send_text(phone, "Hmm, I don't have that photo anymore 🙏 Could you send it again?")
        return

    unlimited = allowlist.has_unlimited_access(phone)
    if not unlimited and credits.get_balance(business_id) < 1:
        await payments.send_topup_options(business_id, phone, prefix="You're out of credits! 🙏 ")
        return

    ctx, last_generation_id = await load_business_context(business_id)
    if last_generation_id:
        await learning.record_accepted_direction(business_id, last_generation_id)

    await send_text(phone, "🎨 Getting this ready... (~15 seconds)")

    try:
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            resp = await http_client.get(reference_image_url)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch stored reference image: {resp.status_code}")

        final_image = image_gen._fit_to_target_size(resp.content)
        if ctx.has_logo:
            async with httpx.AsyncClient(timeout=15.0) as logo_client:
                logo_resp = await logo_client.get(ctx.logo_url)
                if logo_resp.status_code == 200:
                    final_image = await compositor.composite_logo(
                        final_image, logo_resp.content, smart=True, preference=ctx.logo_position_hint,
                    )

        cap = await caption_engine.generate(ctx, "Post this exact uploaded photo as the creative, unedited")

        with get_session() as db:
            gen_row = Generation(
                business_id=business_id,
                user_message="[uploaded photo, used as-is]",
                built_prompt=None,
                status="generating",
                credits_charged=1,
                trigger_source="image_intent_as_is",
            )
            db.add(gen_row)
            db.flush()
            generation_id = gen_row.id

        image_url = await asyncio.to_thread(upload_creative, business_id, generation_id, final_image)
        full_caption = f"{cap['caption']}\n\n{cap['hashtags']}"

        with get_session() as db:
            gen_row = db.query(Generation).filter(Generation.id == generation_id).first()
            gen_row.image_url = image_url
            gen_row.caption = cap["caption"]
            gen_row.hashtags = cap["hashtags"]
            gen_row.status = "done"

            convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
            if convo:
                convo.last_generation_id = generation_id

        image_history.record_image(business_id, "generated", image_url, "the uploaded photo, posted as-is")

        if not unlimited:
            credits.charge_for_generation(business_id, generation_id, amount=1)
        balance = credits.get_balance(business_id)
        low_balance_note = (
            f"\n\n⚠️ Only {balance} credits left. Reply *topup* to recharge."
            if balance <= settings.LOW_BALANCE_THRESHOLD else ""
        )

        from app.engine.orchestrator import _deliver_creative
        await _deliver_creative(phone, business_id, generation_id, image_url, full_caption[:1024])
        await send_text(phone, f"✨ Here's your post!\n\n💳 Credits left: {balance}{low_balance_note}")

    except Exception as exc:
        logger.exception("'Use as-is' delivery failed for business=%s", business_id)
        await alerting.send_alert(
            "use_as_is_failed",
            f"'Use as-is' delivery failed for business={business_id}: {exc!r}",
        )
        await send_text(phone, "Something went wrong 🙏 No credits were charged. Please try again.")
