"""
Feedback loop for the concept-proposal system: when a generation is
accepted, a note about what worked gets saved onto the business's
BrandProfile so future prompt-building calls can reference "this is what
this client responds well to" — the "learnings memory" from the Brand
Brain roadmap, minus a training step: this updates BrandProfile.extras,
never any model weights.

"Accepted" has two triggers, both hooked at the call site, not in here:
  1. app/engine/orchestrator.py — the client moves on to a brand NEW
     generate request without revising the previous one first. Gated by
     quality_score (see MIN_QUALITY_FOR_TACIT_ACCEPT below) — silence
     isn't the same as satisfaction, so a low-scoring generation that
     just didn't get complained about should NOT be recorded as a
     proven-good direction.
  2. app/instagram.py — the client taps "Post to Instagram" on it. An
     explicit, strong signal on its own — bypasses the quality gate
     entirely (require_quality_threshold=False), since choosing to
     publish something publicly is stronger than any score.

Free-revision guard: a free logo-move (credits_charged=0) can become
last_generation_id, but its user_message is a raw positioning instruction
("move logo to top-left"), not creative content — recording it would
pollute learned_preferences/style_summary with noise instead of signal.
Any generation with credits_charged == 0 is skipped here regardless of
quality_score.

Every call writes a LearningEvent row (recorded / skipped_quality /
skipped_no_profile / skipped_free_revision / distilled) — an audit trail
that makes it possible to measure, not guess, whether MIN_QUALITY_FOR_TACIT_ACCEPT
is actually doing its job: compare the NEXT generation's quality_score
following a 'recorded' event vs. following a 'skipped_quality' event.

What gets stored is the client's own request text (Generation.user_message),
not the technical image-gen prompt — short, human-readable, no extra LLM
call needed to produce it.

Distillation: raw preferences are capped at MAX_LEARNED_PREFERENCES. Once
a new entry would exceed the cap, instead of silently dropping the oldest,
one Claude call synthesizes the full set into a compressed style summary
(BrandProfile.extras['style_summary']), and the raw list resets to just
the newest entry. The summary persists and compounds over time — this is
a real first step toward the "Design System Selector" in the Brand Brain
roadmap, not a new idea grafted on.
"""
import json
import logging
import uuid

from anthropic import AsyncAnthropic

from app.config import settings
from app.db import get_session
from app.models import BrandProfile, Generation, Business, LearningEvent

logger = logging.getLogger("socioburp.engine.learning")

client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

MAX_LEARNED_PREFERENCES = 8

# A generation must score at least this well before "the client didn't
# revise it" gets treated as a real endorsement of the direction. Below
# this, silence might just mean they gave up rather than that it worked.
# Deliberately above REGEN_THRESHOLD (60, "good enough to deliver") — not
# every delivered generation is one worth learning from.
MIN_QUALITY_FOR_TACIT_ACCEPT = 75

DISTILL_SYSTEM_PROMPT = """You are distilling a client's accumulated accepted
creative requests into a short, reusable style summary for a marketing agency's
internal reference.

Given a list of past requests this client has responded well to, write ONE
short paragraph (2-4 sentences) capturing the actual PATTERN across them —
recurring themes, mood, colors, what kind of offers/occasions they lean on,
anything a creative director would want to know before starting the next
piece for this client. Do not just list the requests back. Synthesize.

Reply with JSON only, no other text: {"style_summary": "..."}"""


def _log_event(business_id, generation_id, event_type, quality_score=None):
    with get_session() as db:
        db.add(LearningEvent(
            business_id=business_id, generation_id=generation_id,
            event_type=event_type, quality_score=quality_score,
        ))


async def record_accepted_direction(
    business_id: uuid.UUID,
    generation_id: uuid.UUID,
    require_quality_threshold: bool = True,
):
    """
    Call once a generation is considered accepted (see triggers above).
    No-ops (but always logs a LearningEvent) if the business has no
    BrandProfile yet, the generation can't be found, it has no
    user_message, it was a free revision (credits_charged == 0), or (when
    require_quality_threshold=True) its quality_score didn't clear
    MIN_QUALITY_FOR_TACIT_ACCEPT.
    """
    with get_session() as db:
        gen = db.query(Generation).filter(Generation.id == generation_id).first()
        note = gen.user_message.strip()[:200] if gen and gen.user_message else None
        quality_score = gen.quality_score if gen else None
        credits_charged = gen.credits_charged if gen else None

        if not note:
            return

        if credits_charged == 0:
            logger.info(
                "Skipping accept-record for generation=%s — credits_charged=0 "
                "(free revision, e.g. logo move; user_message is an instruction, not creative content)",
                generation_id,
            )
            _log_event(business_id, generation_id, "skipped_free_revision", quality_score)
            return

        if require_quality_threshold and (quality_score is None or quality_score < MIN_QUALITY_FOR_TACIT_ACCEPT):
            logger.info(
                "Skipping accept-record for generation=%s — quality_score=%s below %s, "
                "tacit approval isn't a reliable signal here",
                generation_id, quality_score, MIN_QUALITY_FOR_TACIT_ACCEPT,
            )
            _log_event(business_id, generation_id, "skipped_quality", quality_score)
            return

        business = db.query(Business).filter(Business.id == business_id).first()
        business_name = business.name if business else None
        business_industry = business.industry if business else None

        profile = db.query(BrandProfile).filter(BrandProfile.business_id == business_id).first()
        if profile is None:
            _log_event(business_id, generation_id, "skipped_no_profile", quality_score)
            return

        extras = dict(profile.extras or {})
        learned = list(extras.get("learned_preferences", []))

        if note in learned:
            learned.remove(note)  # move to most-recent instead of duplicating

        would_exceed_cap = len(learned) >= MAX_LEARNED_PREFERENCES

    if would_exceed_cap:
        summary = await _distill_preferences(business_name, business_industry, learned + [note])
        with get_session() as db:
            profile = db.query(BrandProfile).filter(BrandProfile.business_id == business_id).first()
            if profile is None:
                return
            extras = dict(profile.extras or {})
            extras["style_summary"] = summary
            extras["learned_preferences"] = [note]  # reset, keep the newest as a fresh start
            profile.extras = extras
            logger.info("Distilled style summary for business=%s: %r", business_id, summary)
        _log_event(business_id, generation_id, "distilled", quality_score)
        return

    with get_session() as db:
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == business_id).first()
        if profile is None:
            return
        extras = dict(profile.extras or {})
        learned = list(extras.get("learned_preferences", []))
        if note in learned:
            learned.remove(note)
        learned.append(note)
        extras["learned_preferences"] = learned
        # Reassign (not mutate in place) so SQLAlchemy's JSONB change
        # tracking actually picks this up.
        profile.extras = extras
        logger.info("Recorded accepted direction for business=%s: %r", business_id, note)
    _log_event(business_id, generation_id, "recorded", quality_score)


async def _distill_preferences(business_name, business_industry, preferences: list[str]) -> str:
    """
    One Claude call, only fired once every MAX_LEARNED_PREFERENCES accepted
    generations per business — synthesizes raw requests into a reusable
    style paragraph. Fails safe: on any error, falls back to a plain
    joined list rather than losing the signal entirely.
    """
    user_content = (
        f"Business: {business_name or 'Unknown'}, industry: {business_industry or 'Unknown'}\n\n"
        "Past requests this client has responded well to:\n"
        + "\n".join(f"- {p}" for p in preferences)
    )
    try:
        response = await client.messages.create(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=250,
            system=DISTILL_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)
        return parsed["style_summary"]
    except Exception:
        logger.exception("Style distillation failed for business=%r — falling back to plain list", business_name)
        return "Recurring themes in past accepted work: " + "; ".join(preferences)


def get_learned_preferences(business_id: uuid.UUID) -> list[str]:
    """Read-side helper — used when building BusinessContext."""
    with get_session() as db:
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == business_id).first()
        if profile is None or not profile.extras:
            return []
        return list(profile.extras.get("learned_preferences", []))


def get_style_summary(business_id: uuid.UUID) -> str | None:
    """Read-side helper — used when building BusinessContext."""
    with get_session() as db:
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == business_id).first()
        if profile is None or not profile.extras:
            return None
        return profile.extras.get("style_summary")
