"""
Multi-turn carousel negotiation: a "carousel" keyword (see app/router.py)
now asks how many slides (1-9, via a WhatsApp list message) and what each
slide should show, BEFORE generating anything -- replaces the previous
behavior, which silently generated a fixed 3 slides with no per-slide
content and, per the Aug 2026 live-test report, sometimes produced a
single collage image instead of real separate slides when the request
fell through to the old single-image pipeline entirely.

State is tracked on ConversationState.pending_carousel (JSON-in-Text, same
pattern as concept_proposal.py's pending_proposal) so the negotiation
survives across separate incoming messages:
  stage "awaiting_count"          -> waiting for a 1-9 reply
  stage "awaiting_slide_content"  -> waiting for what each slide shows

If a photo is attached to the very first "carousel" message (or to either
reply along the way), it's persisted to R2 immediately -- see
app/storage.py's upload_reference_image() -- since WhatsApp media IDs
aren't guaranteed to stay resolvable for however long the negotiation
takes, and used as the actual base/subject for every slide (see
orchestrator.generate_carousel()'s reference_image handling), not just
described in text.
"""
import asyncio
import json
import logging
import re
import uuid

from app.db import get_session
from app.models import ConversationState
from app.schemas import IncomingMessage
from app.whatsapp.client import send_text, send_list, download_media
from app.storage import upload_reference_image
from app.engine.context import load_business_context
from app import credits, payments, allowlist

logger = logging.getLogger("socioburp.engine.carousel")

from app.config import settings
from app.anthropic_client import create_message

MIN_SLIDES = 1
MAX_SLIDES = 9

COMBINED_SYSTEM_PROMPT = """A client just asked for an Instagram carousel
post, in one message. Figure out what they've already told us so we don't
ask for anything they've already said:

- count: an explicit number of images/slides they stated (e.g. "5
  images", "a carousel of 6", "3 slides"), as an integer. null if they
  did not state a number.
- slides: an array of what EACH slide should show, ONLY if they
  explicitly broke it down into individual items -- a list, numbered, or
  comma-separated (e.g. "product shot, behind-the-scenes, pricing"). null
  if they only described one general theme with no per-item breakdown
  (e.g. "a carousel about our weekend menu" has no per-slide breakdown --
  that's null, not a 1-item list).

Reply with JSON only, no other text: {"count": <int or null>, "slides":
<array of strings, or null>}"""


SLIDES_SYSTEM_PROMPT = """A client is describing what each image in an
Instagram carousel post should show. They may list them as a numbered
list, comma-separated, one per line, or as a single loose sentence.

Split their answer into EXACTLY {count} short, distinct descriptions, one
per slide, in the order given. If they described fewer than {count}
distinct ideas, sensibly extend the list with complementary slide ideas
that fit the same overall theme and business (don't just repeat an
existing one verbatim). If they described more than {count}, keep only
the first {count} in order.

Reply with JSON only, no other text: {{"slides": ["...", "...", ...]}} --
the array must have exactly {count} items."""


async def _parse_slide_briefs(raw_text: str, count: int) -> list[str]:
    """
    Returns exactly `count` distinct per-slide briefs. Falls back to a
    simple delimiter split (and, if still short, padding with the raw
    text) if the Claude call fails -- generation must still be able to
    proceed with SOMETHING for every slide rather than blocking.
    """
    try:
        response = await create_message(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=400,
            system=SLIDES_SYSTEM_PROMPT.format(count=count),
            messages=[{"role": "user", "content": raw_text}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)
        slides = [s.strip() for s in parsed["slides"] if isinstance(s, str) and s.strip()]
        if not slides:
            raise ValueError("Empty slides list")
    except Exception:
        logger.exception("Slide-brief parsing failed for %r — falling back to a naive split", raw_text)
        parts = [p.strip() for p in re.split(r"[\n,;]|(?:^|\s)\d+[).]\s*", raw_text) if p.strip()]
        slides = parts or [raw_text.strip() or "a related image for this business"]

    slides = slides[:count]
    while len(slides) < count:
        slides.append(slides[-1] if slides else raw_text.strip())
    return slides


async def _infer_count_and_slides(raw_text: str) -> tuple[int | None, list[str] | None]:
    """
    Best-effort extraction of a slide count and/or per-slide breakdown
    already present in the client's own opening message -- e.g. "make a
    5-image carousel: product shot, lifestyle, pricing, testimonial,
    behind-the-scenes" needs zero follow-up questions. Keeps the
    negotiation seamless: only ask for whatever's actually still missing,
    never re-ask something already said. Returns (None, None) on any
    failure or genuinely vague request -- start() falls back to the full
    ask-count-then-ask-content flow in that case, same as before this
    existed.

    The returned count is NOT range-clamped here (e.g. "12 images" comes
    back as 12, not None/silently discarded) -- start() checks the range
    itself so it can tell the client their number was noticed but is out
    of range, instead of just re-asking with no explanation.
    """
    if not raw_text or not raw_text.strip():
        return None, None
    try:
        response = await create_message(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=300,
            system=COMBINED_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": raw_text}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)

        count = parsed.get("count")
        if count is not None:
            count = int(count)

        slides = parsed.get("slides")
        if slides is not None:
            slides = [s.strip() for s in slides if isinstance(s, str) and s.strip()]
            slides = slides or None

        return count, slides
    except Exception:
        logger.exception("Combined count/slide inference failed for %r — falling back to asking", raw_text)
        return None, None


def _count_row_label(n: int) -> str:
    if n == 1:
        return "1 image (single post)"
    return f"{n} images"


async def _ask_count(phone: str):
    rows = [(f"carousel_count_{n}", _count_row_label(n)) for n in range(MIN_SLIDES, MAX_SLIDES + 1)]
    await send_list(
        phone,
        "How many images would you like in this carousel? (Instagram carousels post 2 or "
        "more together -- pick 1 if you just want a single post instead.)",
        button_text="Pick a number",
        rows=rows,
        section_title="Number of slides",
    )


def _save_pending(business_id: uuid.UUID, pending: dict):
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        if convo is None:
            convo = ConversationState(business_id=business_id)
            db.add(convo)
        convo.pending_carousel = json.dumps(pending)


def _clear_pending(business_id: uuid.UUID):
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        if convo:
            convo.pending_carousel = None


async def _persist_reference_photo(business_id: uuid.UUID, msg: IncomingMessage) -> str | None:
    if not (msg.type == "image" and msg.media_id):
        return None
    try:
        image_bytes = await download_media(msg.media_id)
        return await asyncio.to_thread(upload_reference_image, business_id, image_bytes)
    except Exception:
        logger.exception("Failed to persist carousel reference photo for business=%s", business_id)
        return None


async def start(business_id: uuid.UUID, msg: IncomingMessage):
    """
    Entry point when 'carousel' is first mentioned (see app/router.py).
    Tries to skip straight to generation (or skip straight to the ONE
    remaining question) when the opening message already said enough --
    see _infer_count_and_slides(). Only falls back to the full
    ask-count-then-ask-content negotiation for a genuinely vague request.
    """
    phone = msg.sender
    unlimited = allowlist.has_unlimited_access(phone)

    if not unlimited and credits.get_balance(business_id) < MIN_SLIDES:
        await payments.send_topup_options(business_id, phone, prefix="You're out of credits! 🙏 ")
        return

    reference_image_url = await _persist_reference_photo(business_id, msg)
    original_message = msg.text or ""

    raw_count, inferred_slides = await _infer_count_and_slides(original_message)
    count_out_of_range = raw_count is not None and not (MIN_SLIDES <= raw_count <= MAX_SLIDES)
    inferred_count = raw_count if not count_out_of_range else None

    if inferred_slides:
        # They already broke it down into per-slide items -- nothing left
        # to ask, generate immediately.
        count = inferred_count or len(inferred_slides)
        count = max(MIN_SLIDES, min(MAX_SLIDES, count))
        if not unlimited and credits.get_balance(business_id) < count:
            await payments.send_topup_options(
                business_id, phone,
                prefix=f"A {count}-image carousel uses {count} credits and you don't have enough right now 🙏 "
            )
            return
        slide_briefs = await _parse_slide_briefs(", ".join(inferred_slides), count)
        ctx, last_generation_id = await load_business_context(business_id)
        from app.engine.orchestrator import generate_carousel
        await generate_carousel(
            business_id, phone, ctx, slide_briefs,
            user_message=original_message, last_generation_id=last_generation_id,
            reference_image_url=reference_image_url,
        )
        return

    if inferred_count:
        # They said how many but not what each one shows -- skip straight
        # to that one remaining question instead of also asking count.
        if not unlimited and credits.get_balance(business_id) < inferred_count:
            await payments.send_topup_options(
                business_id, phone,
                prefix=f"A {inferred_count}-image carousel uses {inferred_count} credits and you don't have enough right now 🙏 "
            )
            return
        pending = {
            "stage": "awaiting_slide_content", "count": inferred_count,
            "original_message": original_message, "reference_image_url": reference_image_url,
        }
        _save_pending(business_id, pending)
        if inferred_count == 1:
            await send_text(phone, "Got it — one image. What should it show?")
        else:
            await send_text(
                phone,
                f"Got it — {inferred_count} images. What should each slide be about? For example: "
                f"\"product shot, behind-the-scenes, pricing, lifestyle shot\" — list them in the "
                f"order you'd like, or just describe the overall idea and I'll split it up.",
            )
        return

    # Genuinely vague ("make me a carousel") -- ask count first. If they
    # DID state a number but it was out of range (e.g. "12 images"), say
    # so explicitly instead of just re-asking with no explanation.
    if count_out_of_range:
        await send_text(phone, f"I can do {MIN_SLIDES}-{MAX_SLIDES} images per carousel — pick a number:")

    pending = {
        "stage": "awaiting_count",
        "original_message": original_message,
        "reference_image_url": reference_image_url,
    }
    _save_pending(business_id, pending)
    await _ask_count(phone)


def _parse_count(msg: IncomingMessage) -> int | None:
    if msg.button_id:
        m = re.fullmatch(r"carousel_count_(\d+)", msg.button_id)
        if m:
            n = int(m.group(1))
            if MIN_SLIDES <= n <= MAX_SLIDES:
                return n
    if msg.text and msg.text.strip().isdigit():
        n = int(msg.text.strip())
        if MIN_SLIDES <= n <= MAX_SLIDES:
            return n
    return None


async def advance(business_id: uuid.UUID, msg: IncomingMessage, pending_raw: str):
    """
    Called for every message while a carousel negotiation is in progress
    (see app/router.py). "cancel" (and any global command or, mid-image-
    upload-negotiation, an explicit carousel request) is intercepted by
    app/router.py itself, via app/engine/router_intent.py's classifier,
    BEFORE this function is ever called -- this function only ever sees a
    message meant as an answer to whatever's pending.
    """
    phone = msg.sender
    unlimited = allowlist.has_unlimited_access(phone)
    pending = json.loads(pending_raw)

    # A photo can arrive on any turn, not just the first -- keep the most
    # recent one.
    new_reference_url = await _persist_reference_photo(business_id, msg)
    if new_reference_url:
        pending["reference_image_url"] = new_reference_url

    stage = pending.get("stage")

    if stage == "awaiting_count":
        count = _parse_count(msg)
        if count is None:
            _save_pending(business_id, pending)  # keep any freshly-persisted photo
            await send_text(phone, f"Please pick a number between {MIN_SLIDES} and {MAX_SLIDES} 🙂")
            await _ask_count(phone)
            return

        if not unlimited and credits.get_balance(business_id) < count:
            _clear_pending(business_id)
            await payments.send_topup_options(
                business_id, phone,
                prefix=f"A {count}-image carousel uses {count} credits and you don't have enough right now 🙏 "
            )
            return

        pending["stage"] = "awaiting_slide_content"
        pending["count"] = count
        _save_pending(business_id, pending)

        if count == 1:
            await send_text(phone, "Got it — one image. What should it show?")
        else:
            await send_text(
                phone,
                f"Got it — {count} images. What should each slide be about? For example: "
                f"\"product shot, behind-the-scenes, pricing, lifestyle shot\" — list them in the "
                f"order you'd like, or just describe the overall idea and I'll split it up.",
            )
        return

    if stage == "awaiting_slide_content":
        count = pending.get("count", MIN_SLIDES)
        if not msg.text or not msg.text.strip():
            if pending.get("reference_image_url") and new_reference_url:
                # A photo with no caption on this turn -- acknowledge it,
                # still need the actual slide content as text.
                _save_pending(business_id, pending)
                await send_text(phone, "Got the photo — and what should each slide be about?")
                return
            await send_text(phone, "Just tell me as text what each slide should show 🙂")
            return

        reference_image_url = pending.get("reference_image_url")
        original_message = pending.get("original_message") or msg.text.strip()
        _clear_pending(business_id)

        slide_briefs = await _parse_slide_briefs(msg.text.strip(), count)
        ctx, last_generation_id = await load_business_context(business_id)

        from app.engine.orchestrator import generate_carousel
        await generate_carousel(
            business_id, phone, ctx, slide_briefs,
            user_message=original_message or msg.text.strip(),
            last_generation_id=last_generation_id, reference_image_url=reference_image_url,
        )
        return

    logger.warning("Unknown pending_carousel stage %r for business=%s — clearing", stage, business_id)
    _clear_pending(business_id)
