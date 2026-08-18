"""
Unified LLM-based classification for app/router.py's top-level message
dispatch -- replaces the keyword-matching cascade that used to live there
(BARE_GREETINGS, UNAMBIGUOUS_GLOBAL_COMMANDS, the carousel keyword,
CANCEL_WORDS, persona.IDENTITY_QUESTION_PATTERNS) with one cheap Haiku
call per message.

Root cause this replaces: the Aug 2026 "carasoul"/"carsoul" incident
traced to an exact-substring carousel check -- but a full audit (see the
Aug 2026 consolidated fix list) found that was one instance of a
systemic pattern, not a one-off: every early-dispatch decision in this
router was an exact or near-exact string match, each with the same
typo/phrasing blind spot. This module replaces the whole cascade at once
rather than patching each keyword list individually.

Scope: used for the router's TOP-LEVEL dispatch (post-onboarding, and to
decide whether an in-progress carousel/image-intent negotiation should be
abandoned for a topic switch -- see app/router.py). NOT onboarding.py's
own state machine (a separate, much lower-traffic flow with its own
skip-word handling, addressed separately) and NOT the deeper
GENERATE/REVISE/QUESTION/OTHER classification inside
orchestrator.generate() (app/engine/intent.py), which still runs
afterward for anything that reaches it -- this module's job is only to
catch the handful of special cases that used to short-circuit BEFORE that
point.

Cost/latency tradeoff, stated plainly: this adds one Haiku call
(CLAUDE_INTENT_MODEL, ~150-300ms, a fraction of a cent) to every
post-onboarding text message, including ones that were previously free
(a bare "hi", "credits", "topup"). Accepted deliberately in exchange for
typo/phrasing tolerance across the whole dispatch, not just carousel.

Fail-safe: on any classifier failure (timeout, malformed JSON, API
error), falls back to _fallback_classify() -- the exact keyword rules
this replaces -- so a classifier hiccup degrades to the OLD,
already-shipped behavior rather than inventing a new failure mode. Empty
text (e.g. an image upload with no caption) skips the Claude call
entirely -- there's nothing to classify.
"""
import difflib
import json
import logging
import re

from app.config import settings

logger = logging.getLogger("socioburp.engine.router_intent")

from app.anthropic_client import create_message
from app.json_extract import extract_json_text

INTENTS = ("GREETING", "IDENTITY_QUESTION", "GLOBAL_COMMAND", "CAROUSEL_REQUEST", "LOGO_UPLOAD", "CANCEL", "OTHER")

SYSTEM_PROMPT = """Classify a WhatsApp message from a small business client
to Sakshi, an AI creative assistant. Tolerate typos, misspellings, and
casual phrasing -- classify by MEANING, not exact wording.

Intents:
- GREETING: a bare hello/hi with no actual request in it (any spelling: "hii", "heyy", "yo")
- IDENTITY_QUESTION: asking whether Sakshi is a real person, an AI, or a bot
- GLOBAL_COMMAND: asking for their credit balance, to top up / buy more credits,
  to see their past generations/history, or to connect their Instagram for
  performance tracking (reach/saves/engagement -- NOT the same as posting).
  Also set "command" to exactly one of "credits" | "topup" | "history" |
  "connect_instagram".
- CAROUSEL_REQUEST: asking for a multi-image Instagram carousel -- a set/series
  of images, multiple slides, or a "collage" of several images meant to be
  posted together (any spelling of "carousel": "carasoul", "carsoul", etc.)
- LOGO_UPLOAD: declaring an attached image AS their business logo, to be
  saved/remembered for future creatives -- not a photo to edit or post
  as-is. Any phrasing that says "this is my logo" / "use this as my logo" /
  "save this logo" / "yeh mera logo hai", optionally with where they'd
  like it placed in the same message ("...put it in the middle").
- CANCEL: explicitly wants to stop/cancel/abandon whatever's currently in
  progress ("never mind", "forget it", "cancel that", "stop")
- OTHER: anything else -- a creative request, a revision, a question about the
  service, casual chat, or unclear. This is the safe default.

Reply with JSON only, no other text:
{"intent": "GREETING|IDENTITY_QUESTION|GLOBAL_COMMAND|CAROUSEL_REQUEST|LOGO_UPLOAD|CANCEL|OTHER", "command": "credits"|"topup"|"history"|"connect_instagram"|null}"""


async def classify(text: str | None) -> dict:
    """Returns {"intent": one of INTENTS, "command": str|None}."""
    if not text or not text.strip():
        return {"intent": "OTHER", "command": None}

    try:
        response = await create_message(
            model=settings.CLAUDE_INTENT_MODEL,
            max_tokens=100,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        out = response.content[0].text.strip()
        out = extract_json_text(out)
        parsed = json.loads(out)

        if parsed.get("intent") not in INTENTS:
            raise ValueError(f"Unexpected intent value: {parsed.get('intent')}")

        return {"intent": parsed["intent"], "command": parsed.get("command")}

    except Exception:
        logger.exception("Router intent classification failed for %r — falling back to keyword rules", text)
        return _fallback_classify(text)


# --- Fallback: the exact rules this module replaces, used ONLY when the
# Claude call itself fails. Deliberately simple/exact rather than typo-
# tolerant beyond what it already had -- it's a safety net for an outage,
# not the primary path.
_BARE_GREETINGS = {"hi", "hey", "hello", "hii", "hiii", "heya", "hola", "yo"}
_GLOBAL_COMMAND_WORDS = {
    "credits": "credits", "balance": "credits", "topup": "topup", "history": "history",
    "connect instagram": "connect_instagram", "connect ig": "connect_instagram",
}
_CANCEL_WORDS = {"cancel", "never mind", "nevermind", "skip", "stop"}
_IDENTITY_QUESTION_PATTERNS = (
    "are you real", "are you a bot", "are you a real person", "are you human",
    "are you ai", "are you an ai", "is this a bot", "are you a person",
    "who are you", "what are you",
)
_CAROUSEL_WORD_RE = re.compile(r"[a-z]+")
_LOGO_UPLOAD_PATTERNS = ("this is my logo", "this is our logo", "use this as my logo", "use this as our logo", "save this logo", "save this as my logo")


def _mentions_carousel(text_lower: str) -> bool:
    if "carousel" in text_lower:
        return True
    for word in _CAROUSEL_WORD_RE.findall(text_lower):
        if len(word) >= 7 and difflib.get_close_matches(word, ["carousel"], n=1, cutoff=0.72):
            return True
    return False


def _fallback_classify(text: str) -> dict:
    text_lower = text.strip().lower()
    greeting_candidate = text_lower.rstrip("!.,?~ ")

    if greeting_candidate in _BARE_GREETINGS:
        return {"intent": "GREETING", "command": None}
    if any(p in text_lower for p in _IDENTITY_QUESTION_PATTERNS):
        return {"intent": "IDENTITY_QUESTION", "command": None}
    if text_lower in _GLOBAL_COMMAND_WORDS:
        return {"intent": "GLOBAL_COMMAND", "command": _GLOBAL_COMMAND_WORDS[text_lower]}
    if _mentions_carousel(text_lower):
        return {"intent": "CAROUSEL_REQUEST", "command": None}
    if any(p in text_lower for p in _LOGO_UPLOAD_PATTERNS):
        return {"intent": "LOGO_UPLOAD", "command": None}
    if text_lower in _CANCEL_WORDS:
        return {"intent": "CANCEL", "command": None}
    return {"intent": "OTHER", "command": None}
