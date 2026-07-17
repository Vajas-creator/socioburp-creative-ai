"""
Cheap, fast intent classification using Haiku. Runs on every post-onboarding
message that isn't a global keyword (credits/topup/history), so it needs to
be fast and cheap — this is NOT where the product's quality comes from.
"""
import json
import logging

from anthropic import AsyncAnthropic

from app.config import settings

logger = logging.getLogger("socioburp.engine.intent")

client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """Classify the user's message into exactly one intent:
- GENERATE: wants a new creative ("create a Diwali post", "make an offer for tomorrow", "post banao kal ke liye")
- REVISE: wants to change the last creative ("make it more premium", "change the color", "use less text", "brighter")
- QUESTION: asking about the service, credits, how things work, or anything not a creative request
- OTHER: greetings, thanks, unclear, or anything that doesn't fit above

Reply with JSON only, no other text: {"intent": "GENERATE|REVISE|QUESTION|OTHER", "brief": "one-line summary of what they want, in English"}"""


async def classify(user_message: str) -> dict:
    """
    Returns {"intent": "GENERATE"|"REVISE"|"QUESTION"|"OTHER", "brief": str}
    Falls back to GENERATE on any parsing failure — better to attempt a
    generation than to silently do nothing when Claude's response is malformed.
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
        return {"intent": "GENERATE", "brief": user_message}
