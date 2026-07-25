"""
Concept proposal step. Runs before any paid image generation happens.

The idea: a real agency doesn't start producing the moment a client says
"I need something for Diwali" — someone proposes a direction first, and
only starts real work once the client agrees. This module is that step.

Two entry points:
  decide()  - called on a fresh GENERATE request. Either returns a proposal
              (specific request needs no proposal, we skip straight past it)
              or signals "specific enough, go straight to generation."
  interpret_reply() - called when a proposal is already pending. Classifies
              the client's reply as CONFIRM (proceed to generation using the
              proposed concept) or ADJUST (client gave feedback — produce a
              revised proposal and stay in the pending state).

Cost note: this entire module is Claude-only, no image generation involved.
That's deliberate — it's the cheapest possible place to avoid a wasted
generation built on a misunderstood brief.
"""
import json
import logging

from anthropic import AsyncAnthropic

from app.config import settings
from app.engine.context import BusinessContext

logger = logging.getLogger("socioburp.engine.concept_proposal")

client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

DECIDE_SYSTEM_PROMPT = """You are a creative director at a marketing agency, deciding
whether a client's request needs a concept discussion first, or is specific enough
to start producing immediately.

A request is SPECIFIC ENOUGH to skip the proposal when it already states what's
needed clearly — occasion/theme, and either an offer detail or a clear visual
direction. Example: "Create a Diwali post, 20% off all items, warm gold tones."

A request NEEDS_PROPOSAL when it's open-ended or vague. Example: "I want something
for Diwali" or "make me a good post" — there's a real creative decision to make
before anything should be produced.

If NEEDS_PROPOSAL, write the actual proposal: a short, concrete creative direction
(theme/mood, color direction, what the headline should focus on, logo placement)
in the voice of a creative director pitching an idea, 2-4 sentences, ending with
a light check-in question. Do not write image-generation prompt language — write
what you'd actually say to a client on WhatsApp.

Reply with JSON only, no other text:
{"decision": "SPECIFIC_ENOUGH", "brief": "..."}
or
{"decision": "NEEDS_PROPOSAL", "proposal_text": "...", "concept_brief": "..."}

concept_brief is a short internal summary of the proposed direction (used later
to actually build the creative) — NOT shown to the client. proposal_text IS
shown to the client, on WhatsApp."""


async def decide(ctx: BusinessContext, user_message: str) -> dict:
    """
    Returns either:
      {"decision": "SPECIFIC_ENOUGH", "brief": str}
      {"decision": "NEEDS_PROPOSAL", "proposal_text": str, "concept_brief": str}
    """
    profile_summary = _summarize_context(ctx)
    user_content = f"Business profile:\n{profile_summary}\n\nClient's request: {user_message}"

    try:
        response = await client.messages.create(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=400,
            system=DECIDE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)

        if parsed.get("decision") not in ("SPECIFIC_ENOUGH", "NEEDS_PROPOSAL"):
            raise ValueError(f"Unexpected decision value: {parsed.get('decision')}")

        return parsed

    except Exception:
        logger.exception("Concept proposal decision failed for: %r", user_message)
        # Fail toward generation rather than getting stuck — worst case the
        # client gets a slightly-off first draft instead of no response at all.
        return {"decision": "SPECIFIC_ENOUGH", "brief": user_message}


INTERPRET_SYSTEM_PROMPT = """A creative director proposed a concept to a client on
WhatsApp. The client just replied. Classify their reply:

CONFIRM - they're approving the proposed direction ("yes", "sounds good", "go
ahead", "love it", "perfect") - even brief/casual confirmations count.

ADJUST - they're giving feedback, asking for a change, or expressing a
preference different from what was proposed, however minor.

If ADJUST, also write a revised proposal (same style as before: 2-4 sentences,
creative director voice, ending with a check-in question) that incorporates
their feedback.

Reply with JSON only, no other text:
{"classification": "CONFIRM"}
or
{"classification": "ADJUST", "proposal_text": "...", "concept_brief": "..."}"""


async def interpret_reply(ctx: BusinessContext, previous_proposal: str, client_reply: str) -> dict:
    """
    Returns either:
      {"classification": "CONFIRM"}
      {"classification": "ADJUST", "proposal_text": str, "concept_brief": str}
    """
    user_content = (
        f"Business profile:\n{_summarize_context(ctx)}\n\n"
        f"Previously proposed concept: {previous_proposal}\n\n"
        f"Client's reply: {client_reply}"
    )

    try:
        response = await client.messages.create(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=400,
            system=INTERPRET_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)

        if parsed.get("classification") not in ("CONFIRM", "ADJUST"):
            raise ValueError(f"Unexpected classification: {parsed.get('classification')}")

        return parsed

    except Exception:
        logger.exception("Proposal reply interpretation failed for: %r", client_reply)
        # Fail toward CONFIRM — if we can't tell, proceeding beats leaving
        # the client stuck in a proposal loop that never resolves.
        return {"classification": "CONFIRM"}


def _summarize_context(ctx: BusinessContext) -> str:
    lines = [f"Business name: {ctx.name or 'Unknown'}", f"Industry: {ctx.industry or 'Unknown'}"]
    if ctx.tone:
        lines.append(f"Brand tone: {ctx.tone}")
    if ctx.target_audience:
        lines.append(f"Target audience: {ctx.target_audience}")
    if ctx.primary_color:
        lines.append(f"Primary color: {ctx.primary_color}")
    return "\n".join(lines)
