"""
Cheap, fast intent classification using Haiku. Runs on every post-onboarding
message that isn't a global keyword (credits/topup/history), so it needs to
be fast and cheap — this is NOT where the product's quality comes from.
"""
import json
import logging


from app.config import settings

logger = logging.getLogger("socioburp.engine.intent")

from app.anthropic_client import create_message
from app.json_extract import extract_json_text

SYSTEM_PROMPT = """Classify the user's message into exactly one intent:
- GENERATE: wants a brand-new creative with no reference to anything earlier
  in this conversation ("create a Diwali post", "make an offer for tomorrow",
  "post banao kal ke liye")
- REVISE: wants to change, build on, or reuse a creative/prompt from EARLIER
  in this conversation. This covers two shapes, both count:
  (a) adjusting the most recent one in place ("make it more premium",
      "change the color", "use less text", "brighter")
  (b) referring back to something from further back, or reusing/repeating
      an earlier request ("use the one from before", "take the prompt from
      our last chat and make one for republic day", "same as last time but
      for Diwali", "edit the image you made", "use the second one", "wahi
      wala jo pehle banaya tha, bas colour badal do")
  If the message points at "earlier", "before", "last time", "that one",
  "the one you made", a specific past item ("the second one"), or asks to
  reuse/repeat a prior prompt/concept in any way, it's REVISE — even
  without an explicit change described, and even if it doesn't literally
  say "change" or "edit".
- QUESTION: asking about the service, credits, how things work, or anything not a creative request
- OTHER: greetings, thanks, unclear, or anything that doesn't fit above

Reply with JSON only, no other text: {"intent": "GENERATE|REVISE|QUESTION|OTHER", "brief": "one-line summary of what they want, in English"}"""


async def classify(user_message: str) -> dict:
    """
    Returns {"intent": "GENERATE"|"REVISE"|"QUESTION"|"OTHER", "brief": str}
    Falls back to OTHER on any parsing failure — a hiccup here (timeout,
    malformed JSON) must never silently spend a client's credit on a guess.
    OTHER sends a generic "how to use me" reply with no charge, which is a
    far better failure mode than treating e.g. "Hi" as a generation request.
    """
    try:
        response = await create_message(
            model=settings.CLAUDE_INTENT_MODEL,
            max_tokens=150,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text.strip()
        text = extract_json_text(text)
        parsed = json.loads(text)

        if parsed.get("intent") not in ("GENERATE", "REVISE", "QUESTION", "OTHER"):
            raise ValueError(f"Unexpected intent value: {parsed.get('intent')}")

        return parsed

    except Exception:
        logger.exception("Intent classification failed for message: %r", user_message)
        return {"intent": "OTHER", "brief": user_message}
