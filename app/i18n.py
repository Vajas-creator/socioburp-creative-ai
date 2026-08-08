"""
Language layer for client-facing messages.

Two mechanisms:
1. Dynamic content (concept proposals, captions) already goes through
   Claude — these get a "respond in {language}" instruction added to
   their existing system prompts. See prompt_builder.py,
   concept_proposal.py, caption.py.
2. Fixed system messages (this module, t()) are translated ONCE per
   (message_key, language) via Claude, then cached in-memory for the life
   of the process — these are constant template strings reused across
   every business using that language, so translating them repeatedly
   per-send would be pure waste. Cache resets on process restart
   (Render redeploy) — acceptable for now; worth persisting to DB if
   translation volume/cost ever becomes meaningful.

LANGUAGE_NAMES maps our internal codes to what we ask Claude to translate
into. 'hinglish' is deliberately not a formal ISO language — it's
Hindi-English code-mixing as commonly typed on WhatsApp by urban/semi-urban
Indian users; Claude is given it as a described style, not a named language.

HONESTY CHECK: translation quality here is Claude-generated, not
human-reviewed. Hindi and Hinglish are the most reliable given training
data volume. Tamil, Telugu, Kannada, and Malayalam translations should be
spot-checked by an actual speaker of each before being trusted at real
client scale — flagging this explicitly rather than presenting uniform
confidence across languages that hasn't actually been earned.
"""
import json
import logging
import re

from anthropic import AsyncAnthropic

from app.config import settings

logger = logging.getLogger("socioburp.i18n")

client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

LANGUAGE_NAMES = {
    "en": "English",
    "hi": "Hindi",
    "hinglish": "Hinglish (Hindi-English code-mixed, as commonly typed on WhatsApp)",
    "ta": "Tamil",
    "te": "Telugu",
    "kn": "Kannada",
    "ml": "Malayalam",
}

DETECT_SYSTEM_PROMPT = """Identify which language this WhatsApp message is written in.
Choose exactly one: en, hi, hinglish, ta, te, kn, ml.
hinglish = Hindi-English code-mixed text (e.g. "kal ka post kaisa raha bhai").
If genuinely unclear, or the message is too short/generic to tell (e.g. just
an emoji, a single common word, a name), reply en — never guess on thin evidence.

Reply with JSON only: {"language": "en"}"""


async def detect_language(text: str) -> str:
    """Fails safe to 'en' on any error, empty input, or unclear signal — never blocks onboarding."""
    if not text or not text.strip():
        return "en"
    try:
        response = await client.messages.create(
            model=settings.CLAUDE_INTENT_MODEL,  # cheap, same tier as intent classification
            max_tokens=50,
            system=DETECT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        text_out = response.content[0].text.strip()
        if text_out.startswith("```"):
            text_out = text_out.strip("`").removeprefix("json").strip()
        parsed = json.loads(text_out)
        lang = parsed.get("language")
        return lang if lang in LANGUAGE_NAMES else "en"
    except Exception:
        logger.exception("Language detection failed for text: %r", text)
        return "en"


_translation_cache: dict[tuple[str, str], str] = {}


async def t(key: str, language: str, english_text: str, **format_kwargs) -> str:
    """
    Returns english_text translated into `language`, cached after first
    translation per (key, language). Always pass the CURRENT english_text
    — if you edit a message's wording later, also bump `key` (e.g.
    "welcome_v2"), or you'll silently get a stale cached translation of
    the old wording.

    format_kwargs are applied via .format() AFTER translation, so dynamic
    values (credit counts, names) never pass through translation
    themselves — only the fixed template shape is translated, once,
    keeping numbers/names untouched and avoiding translation artifacts on
    them.
    """
    if language == "en" or language not in LANGUAGE_NAMES:
        return english_text.format(**format_kwargs) if format_kwargs else english_text

    cache_key = (key, language)
    if cache_key not in _translation_cache:
        try:
            response = await client.messages.create(
                model=settings.CLAUDE_PROMPT_MODEL,
                max_tokens=400,
                system=(
                    f"Translate the following WhatsApp message template into {LANGUAGE_NAMES[language]}. "
                    "Keep it natural and conversational, matching WhatsApp messaging tone — not formal "
                    "or literary. Preserve any {curly_brace_placeholders} EXACTLY as written, unchanged, "
                    "in the same relative position in the sentence. Preserve emoji and *bold* markdown "
                    "markers. Reply with ONLY the translated text, nothing else — no preamble, no quotes."
                ),
                messages=[{"role": "user", "content": english_text}],
            )
            translated = response.content[0].text.strip()

            # Validate every placeholder from the original survived
            # translation, EXACTLY as spelled — str.format() only raises on
            # a malformed placeholder, not a silently DROPPED one, so a
            # translation that quietly loses "{credits}" would otherwise
            # ship incomplete text to a real client with no error at all.
            expected_placeholders = set(re.findall(r"\{(\w+)\}", english_text))
            found_placeholders = set(re.findall(r"\{(\w+)\}", translated))
            if expected_placeholders != found_placeholders:
                logger.warning(
                    "Translation for key=%r language=%r dropped/altered placeholders "
                    "(expected %s, got %s) — falling back to English",
                    key, language, expected_placeholders, found_placeholders,
                )
                translated = english_text

            _translation_cache[cache_key] = translated
        except Exception:
            logger.exception("Translation failed for key=%r language=%r — falling back to English", key, language)
            _translation_cache[cache_key] = english_text

    template = _translation_cache[cache_key]
    try:
        return template.format(**format_kwargs) if format_kwargs else template
    except (KeyError, IndexError):
        # Belt-and-suspenders: catches a malformed (not just missing)
        # placeholder that somehow passed the check above.
        logger.warning(
            "Translated template for key=%r language=%r broke a format placeholder — falling back to English",
            key, language,
        )
        return english_text.format(**format_kwargs) if format_kwargs else english_text


def get_preferred_language(business_id) -> str:
    """Read-side helper. Falls back to 'en' if unset or the business row is missing."""
    from app.db import get_session
    from app.models import Business
    with get_session() as db:
        biz = db.query(Business).filter(Business.id == business_id).first()
        return biz.preferred_language if biz and biz.preferred_language else "en"
