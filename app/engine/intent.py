"""
Cheap, fast intent classification using Haiku. Runs on every post-onboarding
message that isn't a global keyword (credits/topup/history), so it needs to
be fast and cheap — this is NOT where the product's quality comes from.
"""
import json
import logging


from app.config import settings

logger = logging.getLogger("socioburp.engine.intent")

from app.anthropic_client import client

SYSTEM_PROMPT = """Classify the user's message into exactly one intent:
- GENERATE: wants a new creative ("create a Diwali post", "make an offer for tomorrow", "post banao kal ke liye")
- REVISE: wants to change the last creative ("make it more premium", "change the color", "use less text", "brighter")
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
        response = await client.messages.create(
            model=settings.CLAUDE_INTENT_MODEL,
            max_tokens=150,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text.strip()
        # Strip markdown fences if Claude adds them despite instructions
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)

        if parsed.get("intent") not in ("GENERATE", "REVISE", "QUESTION", "OTHER"):
            raise ValueError(f"Unexpected intent value: {parsed.get('intent')}")

        return parsed

    except Exception:
        logger.exception("Intent classification failed for message: %r", user_message)
        return {"intent": "OTHER", "brief": user_message}
