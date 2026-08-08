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
from app.i18n import LANGUAGE_NAMES
from app.persona import PERSONA_SYSTEM_FRAGMENT

logger = logging.getLogger("socioburp.engine.concept_proposal")

client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

DECIDE_SYSTEM_PROMPT = f"""{PERSONA_SYSTEM_FRAGMENT}

You're deciding whether a client's request needs a concept discussion first,
or is specific enough to start producing immediately.

A request is SPECIFIC ENOUGH to skip the proposal when it already states what's
needed clearly — occasion/theme, and either an offer detail or a clear visual
direction. Example: "Create a Diwali post, 20% off all items, warm gold tones."

A request NEEDS_PROPOSAL when it's open-ended or vague. Example: "I want something
for Diwali" or "make me a good post" — there's a real creative decision to make
before anything should be produced.

If NEEDS_PROPOSAL, identify the SPECIFIC missing details that would most improve
accuracy: offer specifics (discount %, dates, or key message), visual mood/color
direction if not already fixed by the brand profile, and anything that must be
included (a phone number, a specific product, a promotion detail). Only ask about
what's actually missing — never ask about something already known from the brand
profile or from "Distilled style pattern" / "Recent requests this client has
responded well to" if present. Weave 1-3 direct clarifying questions naturally into 2-4 sentences,
as Maya pitching a direction — not a form, not a single generic "sound good?" Do
not write image-generation prompt language — write what you'd actually say to a
client on WhatsApp.

Reply with JSON only, no other text:
{{"decision": "SPECIFIC_ENOUGH", "brief": "..."}}
or
{{"decision": "NEEDS_PROPOSAL", "proposal_text": "...", "concept_brief": "..."}}

concept_brief is a short internal summary of the proposed direction (used later
to actually build the creative) — NOT shown to the client. proposal_text IS
shown to the client, on WhatsApp, as Maya."""


async def decide(ctx: BusinessContext, user_message: str) -> dict:
    """
    Returns either:
      {"decision": "SPECIFIC_ENOUGH", "brief": str}
      {"decision": "NEEDS_PROPOSAL", "proposal_text": str, "concept_brief": str}
    """
    profile_summary = _summarize_context(ctx)
    user_content = f"Business profile:\n{profile_summary}\n\nClient's request: {user_message}"
    if ctx.language and ctx.language != "en" and ctx.language in LANGUAGE_NAMES:
        user_content += f"\n\nWrite proposal_text in {LANGUAGE_NAMES[ctx.language]} (concept_brief stays in English — it's internal-only)."

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
        # Fail safe, not toward generation: this call itself failed, so there
        # is no real proposal to act on. Ask for clarification instead of
        # guessing and spending a credit — reuses the existing NEEDS_PROPOSAL
        # flow with a hardcoded question rather than a model-written one.
        return {
            "decision": "NEEDS_PROPOSAL",
            "proposal_text": (
                "Sorry, I didn't quite catch the details there 🙏 Could you tell me a "
                "bit more about what you'd like — the occasion, any offer details, or "
                "the general vibe you're going for?"
            ),
            "concept_brief": user_message,
        }


INTERPRET_SYSTEM_PROMPT = f"""{PERSONA_SYSTEM_FRAGMENT}

You (Maya) proposed a concept to a client on WhatsApp. The client just replied.
Classify their reply:

CONFIRM - they're approving the proposed direction ("yes", "sounds good", "go
ahead", "love it", "perfect") - even brief/casual confirmations count.

ADJUST - they're giving feedback, asking for a change, or expressing a
preference different from what was proposed, however minor.

If ADJUST, also write a revised proposal (same style as before: 2-4 sentences,
as Maya, ending with a check-in question) that incorporates their feedback.

Reply with JSON only, no other text:
{{"classification": "CONFIRM"}}
or
{{"classification": "ADJUST", "proposal_text": "...", "concept_brief": "..."}}"""


async def interpret_reply(ctx: BusinessContext, previous_proposal: str, client_reply: str) -> dict:
    """
    Returns one of:
      {"classification": "CONFIRM"}
      {"classification": "ADJUST", "proposal_text": str, "concept_brief": str}
      {"classification": "RETRY"}  -- only on failure; see except block below
    """
    user_content = (
        f"Business profile:\n{_summarize_context(ctx)}\n\n"
        f"Previously proposed concept: {previous_proposal}\n\n"
        f"Client's reply: {client_reply}"
    )
    if ctx.language and ctx.language != "en" and ctx.language in LANGUAGE_NAMES:
        user_content += f"\n\nIf ADJUST, write proposal_text in {LANGUAGE_NAMES[ctx.language]} (concept_brief stays in English)."

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
        # Fail safe, not toward CONFIRM: auto-approving on a failed call means
        # silently charging a credit and generating on a guess. RETRY asks
        # again instead — the caller (orchestrator) leaves pending_proposal
        # untouched, so the client's next reply gets a fresh attempt.
        return {"classification": "RETRY"}


def _summarize_context(ctx: BusinessContext) -> str:
    lines = [f"Business name: {ctx.name or 'Unknown'}", f"Industry: {ctx.industry or 'Unknown'}"]
    if ctx.tone:
        lines.append(f"Brand tone: {ctx.tone}")
    if ctx.target_audience:
        lines.append(f"Target audience: {ctx.target_audience}")
    if ctx.primary_color:
        lines.append(f"Primary color: {ctx.primary_color}")
    if ctx.style_summary:
        lines.append(f"Distilled style pattern for this client: {ctx.style_summary}")
    if ctx.learned_preferences:
        lines.append("Recent requests this client has responded well to (for style/direction reference):")
        for pref in ctx.learned_preferences:
            lines.append(f"  - {pref}")
    if ctx.industry_style:
        lines.append(f"Current industry trends: {ctx.industry_style}")
    return "\n".join(lines)
