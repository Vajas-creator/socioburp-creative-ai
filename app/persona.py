"""
Sakshi — SocioBurp's persona. One name, one voice, used consistently across
every client-facing touchpoint instead of the previous patchwork where
onboarding, proposals, and error messages each had a slightly different
unstated tone because they were written at different times.

Scope note: Sakshi's voice applies to messages FROM SocioBurp TO the client
(onboarding, proposals, delivery, errors). It does NOT apply to
app/engine/caption.py — that generates the CLIENT's own Instagram caption,
in the client's brand voice, going out to the client's customers. That's
not Sakshi talking; conflating the two would put SocioBurp's voice into a
client's own social media post, which is wrong.

PERSONA_SYSTEM_FRAGMENT is meant to be appended/interpolated into system
prompts for Claude calls that produce client-facing text (see
concept_proposal.py). ANNOUNCE_TEXT / DISCLOSURE_TEXT are static template
strings (translated via app.i18n.t, same as other fixed messages).
"""
import re

PERSONA_NAME = "Sakshi"

PERSONA_SYSTEM_FRAGMENT = """You are Sakshi, a creative partner at SocioBurp — a
specific, consistent persona, not a generic assistant. Voice: warm, competent,
a little informal, like a skilled creative director who respects the client's
time. Not corporate, not robotic, not overly bubbly. You genuinely care
whether their creative looks good and whether it actually helps their
business. Write as Sakshi, in first person, when addressing the client
directly."""

# Static template — goes through app.i18n.t() like other fixed messages, so
# it appears in the client's detected/confirmed language.
DISCLOSURE_TEXT = (
    "I'm Sakshi, your creative partner at SocioBurp 👋 I'm actually an AI — "
    "trained specifically on great creative direction for businesses like "
    "yours. Not human, but genuinely here to make your posts look great!"
)

# Lightweight keyword match, checked before the AI intent classifier so this
# never misfires into the generic "I'm your creative assistant" fallback.
# Deliberately simple/exact-ish rather than a full classifier — a narrow,
# low-frequency question class where a keyword list covers the common
# phrasings; less common phrasings fall through to normal QUESTION/OTHER
# handling, a known and accepted gap rather than over-building this.
IDENTITY_QUESTION_PATTERNS = (
    "are you real", "are you a bot", "are you a real person", "are you human",
    "are you ai", "are you an ai", "is this a bot", "are you a person",
    "who are you", "what are you",
)


def is_identity_question(text: str) -> bool:
    text_lower = (text or "").strip().lower()
    return any(p in text_lower for p in IDENTITY_QUESTION_PATTERNS)


# Mid-conversation name statements ("by the way, my name's Priya") -- not
# during onboarding's own dedicated name question (see app/onboarding.py,
# a separate state-machine step), but a client volunteering it later, any
# time. Previously this had nowhere to go: intent_engine.classify() calls
# it OTHER, and orchestrator.generate() sent the generic "I'm Sakshi, your
# creative partner here! Try something like..." menu reply right back --
# ignoring what they just said, which reads as not listening. Deliberately
# a narrow, keyword-anchored regex, not a Claude call -- same reasoning as
# IDENTITY_QUESTION_PATTERNS above: covers the common explicit phrasings,
# and a less common one just falls through to the normal OTHER handling
# (a known, accepted gap, not a silent failure).
_NAME_STATEMENT_PATTERNS = (
    re.compile(r"\bmy name(?:'s| is)\s+([A-Za-z][A-Za-z'\-]{1,30})", re.IGNORECASE),
    re.compile(r"\byou can call me\s+([A-Za-z][A-Za-z'\-]{1,30})", re.IGNORECASE),
    re.compile(r"\bcall me\s+([A-Za-z][A-Za-z'\-]{1,30})", re.IGNORECASE),
)

# "call me" is also common in phrasings that aren't a name at all ("call me
# back", "call me later/tomorrow") -- a plain regex can't tell those apart
# from "call me Dev", so anything captured that's one of these gets
# rejected rather than saved as someone's name.
_NAME_STOPWORDS = {"back", "later", "tomorrow", "today", "tonight", "now", "soon", "anytime", "please", "when"}


def extract_stated_name(text: str) -> str | None:
    """Best-effort extraction of a name volunteered mid-conversation, or None."""
    text = (text or "").strip()
    if not text:
        return None
    for pattern in _NAME_STATEMENT_PATTERNS:
        m = pattern.search(text)
        if m:
            name = m.group(1).strip(" .,!?'-")
            if name and name.lower() not in _NAME_STOPWORDS:
                return name.capitalize()
    return None
