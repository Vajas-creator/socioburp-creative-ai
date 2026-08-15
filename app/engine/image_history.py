"""
Short-term conversational memory of recent images (both photos a client
uploads AND creatives Sakshi generates) per business, so "change that
background" or "use the second one" can resolve against real context
instead of forcing a re-upload or blindly assuming "the last thing." See
ConversationState.recent_images (JSONB), added for this.

Deliberately a short, capped list (MAX_HISTORY most recent, newest last)
-- conversational reference memory, not a full history browser (that's
app/history.py, backed directly by the Generation table for the
client-facing "history" command).

resolve_reference() is a no-op (returns None) whenever there's fewer than
2 images in history -- the existing "most recent" default (last_generation_id,
already used throughout orchestrator.py) already handles the single-image
case correctly and cheaply; this module only needs to do real work once
there's genuine ambiguity to resolve.
"""
import json
import logging
import uuid

from app.config import settings
from app.db import get_session
from app.models import ConversationState

logger = logging.getLogger("socioburp.engine.image_history")

from app.anthropic_client import create_message
from app.json_extract import extract_json_text

MAX_HISTORY = 8

RESOLVE_SYSTEM_PROMPT = """A client is replying to Sakshi, a WhatsApp
creative assistant, possibly about an image from earlier in this
conversation. Below is a numbered list of recent images (both photos the
client uploaded and creatives Sakshi generated), oldest first.

Decide which image, if any, the client's message clearly refers to.

Reply with JSON only, no other text:
{"index": <1-based index into the list, or null if the message doesn't
clearly reference one specific past image -- e.g. it's about the most
recent thing by default, or isn't about a past image at all>}"""


def record_image(business_id: uuid.UUID, kind: str, url: str, label: str):
    """kind: 'generated' | 'uploaded'. label: a short human description, used only for reference resolution."""
    if not url:
        return
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        if convo is None:
            convo = ConversationState(business_id=business_id)
            db.add(convo)
            db.flush()
        history = list(convo.recent_images or [])
        history.append({"kind": kind, "url": url, "label": (label or "")[:200]})
        convo.recent_images = history[-MAX_HISTORY:]


def get_history(business_id: uuid.UUID) -> list[dict]:
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        return list(convo.recent_images) if convo and convo.recent_images else []


async def resolve_reference(business_id: uuid.UUID, text: str) -> dict | None:
    """
    Returns {"kind": ..., "url": ..., "label": ...} for the specific past
    image the message refers to, or None if there's nothing to
    disambiguate (fewer than 2 images in history) or the message doesn't
    clearly point at one -- callers should fall back to their existing
    default (most recent) in either case, same as before this existed.
    """
    history = get_history(business_id)
    if len(history) < 2 or not text or not text.strip():
        return None

    listing = "\n".join(f"{i + 1}. ({h.get('kind')}) {h.get('label') or '(no description)'}" for i, h in enumerate(history))

    try:
        response = await create_message(
            model=settings.CLAUDE_INTENT_MODEL,
            max_tokens=50,
            system=RESOLVE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Recent images:\n{listing}\n\nClient's message: {text}"}],
        )
        out = response.content[0].text.strip()
        out = extract_json_text(out)
        parsed = json.loads(out)

        idx = parsed.get("index")
        if idx is None:
            return None
        idx = int(idx)
        if 1 <= idx <= len(history):
            return history[idx - 1]
        return None

    except Exception:
        logger.exception("Image reference resolution failed for business=%s", business_id)
        return None
