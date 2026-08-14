"""
Two one-time, persona-voiced messages from Sakshi, written by Claude rather
than a fixed Python template — consistent with how every other
client-facing message in this codebase that needs to sound like an actual
person (concept_proposal.py's proposals, caption.py) is written by Claude,
not string-substituted. See app/persona.py for the shared voice.

understand_business() — sent once, right after a client answers "What
does your business do?" (app/onboarding.py's "awaiting_business_description"
state): "Got it. You run a X. I'm going to remember that your brand needs
to feel Y... One more thing..." Works directly from their raw free-text
answer (not pre-extracted structured fields — there's nothing else to
work from yet at this point in the shorter, two-question onboarding
flow), and also extracts business_type/business_name for storage on the
Business row.

reflect_first_result() — sent once, right before a business's very
first-ever generation (app/engine/orchestrator.py's _run_generation(),
gated on last_generation_id is None): "I've got a pretty good idea of
your brand now. There's one thing I think we can improve: ..." Note there
is no literal Instagram content to look at unless the client uploaded an
actual screenshot (in which case app/engine/color_discovery.py already
extracted real colors from it, which flow into BusinessContext) — if they
only sent a handle/link, there is nothing real to observe, and the system
prompt is explicit that Sakshi must never claim to have seen a post that
was never actually fetched.

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

UNDERSTAND_BUSINESS_SYSTEM_PROMPT = f"""{PERSONA_SYSTEM_FRAGMENT}

A client just answered "What does your business do?" in their own words.
From that answer, extract:

- business_type: a natural, specific description of what they do (2-5
  words, e.g. "handmade gifting business", "family-run bakery", "boutique
  hair salon") — not a generic category, their actual words distilled.
- brand_adjectives: 2-4 words describing how the brand should FEEL (not
  what it sells) — inferred from how they described it. Specific and
  evocative, not generic filler like "great" or "nice".
- business_name: their business's actual NAME, ONLY if they clearly stated
  one in their answer (e.g. "I run Copper & Crumb, a bakery" -> "Copper &
  Crumb"). null if no name was given — do not invent one.

Then write Sakshi's message reflecting this back, in EXACTLY this
structure — five lines, keep the line breaks, only the bracketed parts
vary:

Got it.
You run a [business_type].
I'm going to remember that your brand needs to feel [brand_adjectives] — not like a mass-produced catalogue.
One more thing...

Do not add any other lines, preamble, or sign-off. Reply with JSON only,
no other text: {{"business_type": "...", "brand_adjectives": "...", "business_name": "..." or null, "message": "..."}}"""


async def understand_business(description: str, language: str = "en") -> dict:
    """
    Returns {"business_type": str, "brand_adjectives": str,
    "business_name": str | None, "message": str} — message is the fully
    composed text, ready to send as-is. Falls back to a plain,
    still-on-brief version (using their raw answer as business_type) if
    the Claude call fails, so onboarding can still proceed.
    """
    user_content = f"Client's answer: {description}"
    if language and language != "en" and language in LANGUAGE_NAMES:
        user_content += f"\n\nWrite the message in {LANGUAGE_NAMES[language]}, keeping the same five-line structure."

    try:
        response = await create_message(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=250,
            system=UNDERSTAND_BUSINESS_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)
        message = parsed.get("message", "").strip()
        if not message or not parsed.get("business_type"):
            raise ValueError("Missing or empty required fields in response")
        return {
            "business_type": parsed["business_type"],
            "brand_adjectives": parsed.get("brand_adjectives", ""),
            "business_name": parsed.get("business_name") or None,
            "message": message,
        }
    except Exception:
        logger.exception("understand_business failed for description=%r", description)
        # Don't prepend "You run a" to their raw answer -- it often already
        # starts with "I run..."/"We do...", producing "You run a I run a
        # hair salon" if just concatenated. business_type stored on the
        # Business row still uses the raw text (still useful data), but the
        # user-facing fallback message avoids that specific grammatical trap.
        business_type = description.strip()[:60] or "business"
        return {
            "business_type": business_type,
            "brand_adjectives": "",
            "business_name": None,
            "message": (
                "Got it.\n"
                "I'm going to remember what makes your brand distinct — not like a mass-produced catalogue.\n"
                "One more thing..."
            ),
        }


FIRST_RESULT_SYSTEM_PROMPT = f"""{PERSONA_SYSTEM_FRAGMENT}

A client just finished onboarding and is about to receive their FIRST-EVER
generated creative. Before it's ready, Sakshi sends one message noticing a
specific, plausible gap between how a business like theirs typically
presents online and the brand identity they just described — something
worth improving in this first piece.

IMPORTANT: you do NOT have their actual Instagram content to look at,
UNLESS real extracted brand colors are listed below (those came from an
actual screenshot they uploaded — genuine signal, safe to reference
directly, e.g. "your current colors feel more X than Y"). If no colors
are listed, even if an Instagram handle/link is mentioned, you have NOT
seen that account — ground the observation in their stated business type,
tone, and general industry context only, and never claim to have viewed a
real post of theirs.

Write it in EXACTLY this structure — five lines, keep the line breaks,
only the bracketed part varies:

I've got a pretty good idea of your brand now.
There's one thing I think we can improve:
[specific observation about their content vs brand]
So I want to try something different.
Give me a moment.

[specific observation]: ONE concrete, plausible sentence naming a specific
gap — grounded in the profile details actually given, not a vague
platitude like "your posts could be better".

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
        subject = ctx.industry or "your business"
        return (
            "I've got a pretty good idea of your brand now.\n"
            "There's one thing I think we can improve:\n"
            f"Content for a {subject} like yours often reads generic — I want to make yours actually feel like you.\n"
            "So I want to try something different.\n"
            "Give me a moment."
        )


EXTRACT_BRAND_DETAILS_SYSTEM_PROMPT = """A client just answered an
optional onboarding question: "Any brand colors, price range, or style
dos/don'ts I should know?" Extract structured fields from their free-text
answer:

- primary_color: a hex color code (e.g. "#C4453D"), ONLY if they clearly
  named or described one specific color (translate a named color like
  "burgundy" or "forest green" to its closest hex). null if no color was
  clearly given.
- secondary_color: same rules, for a second color if one was given. null
  otherwise.
- positioning_notes: a short (1-2 sentence) distillation of anything about
  price range/positioning (e.g. "premium, not budget") and style dos/
  don'ts (e.g. "never use pink, keep it minimal") they mentioned. null if
  they only gave colors and nothing else.

Reply with JSON only, no other text:
{"primary_color": "#RRGGBB" or null, "secondary_color": "#RRGGBB" or null, "positioning_notes": "..." or null}"""


async def extract_brand_details(text: str) -> dict:
    """
    Returns {"primary_color": str|None, "secondary_color": str|None,
    "positioning_notes": str|None}. Falls back to all-None on any failure
    -- onboarding must still complete even if this extraction fails, it's
    purely a bonus enrichment.
    """
    try:
        response = await create_message(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=200,
            system=EXTRACT_BRAND_DETAILS_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        out = response.content[0].text.strip()
        if out.startswith("```"):
            out = out.strip("`").removeprefix("json").strip()
        parsed = json.loads(out)
        return {
            "primary_color": parsed.get("primary_color") or None,
            "secondary_color": parsed.get("secondary_color") or None,
            "positioning_notes": parsed.get("positioning_notes") or None,
        }
    except Exception:
        logger.exception("extract_brand_details failed for text=%r", text)
        return {"primary_color": None, "secondary_color": None, "positioning_notes": None}


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
    if ctx.instagram_handle:
        lines.append(f"Instagram page given (not fetched/viewed): {ctx.instagram_handle}")
    return "\n".join(lines)
