"""
Two one-time, persona-voiced messages from Maya, written by Claude rather
than a fixed Python template — consistent with how every other
client-facing message in this codebase that needs to sound like an actual
person (concept_proposal.py's proposals, caption.py) is written by Claude,
not string-substituted. See app/persona.py for the shared voice.

reflect_understanding() — sent once, right when onboarding completes
(app/onboarding.py's "awaiting_tone" branch): "Got it. You run a X. I'm
going to remember that your brand needs to feel Y..." Establishes that
Maya actually processed what they told her, not just recorded it.

reflect_first_result() — sent once, right before a business's very
first-ever generation (app/engine/orchestrator.py's _run_generation(),
gated on last_generation_id is None): "I've got a pretty good idea of
your brand now. There's one thing I think we can improve: ..." Note there
is no literal "before" content to diff against — this app doesn't retain
a client's Instagram grid or any external content, only their onboarding
profile (industry, tone, colors, target audience) and, if their industry
has been researched, general industry trend context. The "observation" is
Maya's best specific-sounding read of a plausible gap from what she does
know, not a scan of real external content — framed that way in the system
prompt below so it stays plausible rather than inventing false specifics.

Both keep the required message structure (line breaks, fixed phrasing)
explicit in the prompt, varying only the bracketed content, same
discipline as prompt_builder.py's headline-length rules.
"""
import json
import logging

from app.config import settings
from app.engine.context import BusinessContext
from app.i18n import LANGUAGE_NAMES
from app.persona import PERSONA_SYSTEM_FRAGMENT

logger = logging.getLogger("socioburp.engine.brand_reflection")

from app.anthropic_client import create_message

UNDERSTANDING_SYSTEM_PROMPT = f"""{PERSONA_SYSTEM_FRAGMENT}

A client just finished onboarding (name, industry, logo, colors, brand
vibe). Write Maya's message reflecting that understanding back to them, in
EXACTLY this structure — four lines, keep the line breaks, only the
bracketed parts vary:

Got it.
You run a [business type].
I'm going to remember that your brand needs to feel [brand adjectives] — not like a mass-produced catalogue.

[business type]: a natural, specific description of what they do, derived
from their industry and any other signal available (2-5 words, e.g.
"handmade gifting business", "family-run bakery", "boutique hair salon")
— not just the raw industry category word.

[brand adjectives]: 2-4 words describing how the brand should FEEL (not
what it sells), derived from their chosen brand tone, colors, target
audience, and industry context. Specific and evocative, not generic
filler like "great" or "nice".

Do not add any other lines, preamble, or sign-off. Reply with JSON only,
no other text: {{"message": "..."}}"""


async def reflect_understanding(ctx: BusinessContext) -> str:
    """
    Returns the fully composed message text, ready to send as-is.
    Falls back to a plain Python-templated version (still on-brief, just
    less specific) if the Claude call fails.
    """
    user_content = f"Business profile:\n{_summarize_context(ctx)}"
    if ctx.language and ctx.language != "en" and ctx.language in LANGUAGE_NAMES:
        user_content += f"\n\nWrite the message in {LANGUAGE_NAMES[ctx.language]}, keeping the same four-line structure."

    try:
        response = await create_message(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=200,
            system=UNDERSTANDING_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)
        message = parsed.get("message", "").strip()
        if not message:
            raise ValueError("Missing or empty 'message' in response")
        return message
    except Exception:
        logger.exception("reflect_understanding failed for industry=%r", ctx.industry)
        business_type = ctx.industry or "business"
        adjectives = ctx.tone or "distinctly yours"
        return (
            "Got it.\n"
            f"You run a {business_type}.\n"
            f"I'm going to remember that your brand needs to feel {adjectives} — not like a mass-produced catalogue."
        )


FIRST_RESULT_SYSTEM_PROMPT = f"""{PERSONA_SYSTEM_FRAGMENT}

A client just finished onboarding and is about to receive their FIRST-EVER
generated creative. Before it's ready, Maya sends one message noticing a
specific, plausible gap between how a business like theirs typically
presents online and the brand identity they just described — something
worth improving in this first piece. You do NOT have their actual
Instagram content to look at; ground the observation in their profile
(industry, tone, colors, target audience) and general industry context
only — plausible and specific, never claiming to have seen a real post of
theirs. Write it in EXACTLY this structure — five lines, keep the line
breaks, only the bracketed part varies:

I've got a pretty good idea of your brand now.
There's one thing I think we can improve:
[specific observation about their content vs brand]
So I want to try something different.
Give me a moment.

[specific observation]: ONE concrete, plausible sentence naming a specific
gap — e.g. generic stock-photo energy vs a stated premium/handmade
identity, a common industry pattern that undersells their chosen brand
tone, or a mismatch between their target audience and what typically gets
posted in their industry. Specific and grounded in the profile details
given, not a vague platitude like "your posts could be better".

Do not add any other lines, preamble, or sign-off. Reply with JSON only,
no other text: {{"message": "..."}}"""


async def reflect_first_result(ctx: BusinessContext) -> str:
    """
    Returns the fully composed message text, ready to send as-is. Called
    once, right before a business's very first-ever generation (see
    app/engine/orchestrator.py). Falls back to a plain Python-templated
    version if the Claude call fails.
    """
    user_content = f"Business profile:\n{_summarize_context(ctx)}"
    if ctx.language and ctx.language != "en" and ctx.language in LANGUAGE_NAMES:
        user_content += f"\n\nWrite the message in {LANGUAGE_NAMES[ctx.language]}, keeping the same five-line structure."

    try:
        response = await create_message(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=200,
            system=FIRST_RESULT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)
        message = parsed.get("message", "").strip()
        if not message:
            raise ValueError("Missing or empty 'message' in response")
        return message
    except Exception:
        logger.exception("reflect_first_result failed for industry=%r", ctx.industry)
        tone = ctx.tone or "distinctive"
        return (
            "I've got a pretty good idea of your brand now.\n"
            "There's one thing I think we can improve:\n"
            f"Your content could lean more into feeling {tone}, rather than generic.\n"
            "So I want to try something different.\n"
            "Give me a moment."
        )


def _summarize_context(ctx: BusinessContext) -> str:
    lines = [f"Business name: {ctx.name or 'Unknown'}", f"Industry: {ctx.industry or 'Unknown'}"]
    if ctx.tone:
        lines.append(f"Brand tone: {ctx.tone}")
    if ctx.primary_color:
        lines.append(f"Primary color: {ctx.primary_color}")
    if ctx.secondary_color:
        lines.append(f"Secondary color: {ctx.secondary_color}")
    if ctx.target_audience:
        lines.append(f"Target audience: {ctx.target_audience}")
    if ctx.industry_style:
        lines.append(f"Current industry trends: {ctx.industry_style}")
    return "\n".join(lines)
