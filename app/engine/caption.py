"""
Caption + hashtag generation. Runs after the image is chosen, using the
notes_for_caption from the prompt builder (offer details, dates, etc. that
didn't belong baked into the image itself).

NOTE: takes BusinessContext (plain data), not live ORM objects — see
app/engine/context.py for why.
"""
import json
import logging


from app.config import settings
from app.engine.context import BusinessContext
from app.i18n import LANGUAGE_NAMES

logger = logging.getLogger("socioburp.engine.caption")

from app.anthropic_client import create_message

SYSTEM_PROMPT = """Write an Instagram caption for a social media marketing creative,
for an Indian small business owner to post as themselves.

Write it like the business owner is texting a customer about this directly
— not formal ad copy, not a structured Hook/CTA template, not a paragraph.
Short, plain, conversational sentences (contractions are fine — "we've",
"don't", "it's"). One clear idea: what it is, why it's worth it, done. Fold
the call to action naturally into that instead of a separate labelled line.

1-3 short sentences total, under 40 words (not counting hashtags). 1-2 emoji
max, used naturally, not decoratively. If it reads like something a real
person would actually type on their phone in twenty seconds, it's right; if
it reads like an ad brief, has bullet-style structure, or sounds like a
marketing template, it's wrong — simpler is always better here.

After the caption, a blank line, then 8-12 hashtags mixing niche +
local/city + broad reach tags. Hashtags stay in English/Latin script
regardless of caption language — that's how they're actually searched on
Instagram.

Reply with JSON only, no other text: {"caption": "...", "hashtags": "..."}"""


async def generate(ctx: BusinessContext, notes_for_caption: str) -> dict:
    """
    Returns {"caption": str, "hashtags": str}
    """
    context_lines = [
        f"Business: {ctx.name or 'this business'}, industry: {ctx.industry or 'general'}",
    ]
    if ctx.tone:
        context_lines.append(f"Tone: {ctx.tone}")
    if ctx.target_audience:
        context_lines.append(f"Audience: {ctx.target_audience}")
    if ctx.contact_phone:
        context_lines.append(f"Contact: {ctx.contact_phone}")
    if ctx.website:
        context_lines.append(f"Website: {ctx.website}")
    context_lines.append(f"Offer/details to include: {notes_for_caption}")

    language = ctx.language or "en"
    if language != "en" and language in LANGUAGE_NAMES:
        context_lines.append(
            f"Write the caption itself in {LANGUAGE_NAMES[language]} (hashtags stay in English as noted above)."
        )

    user_content = "\n".join(context_lines)

    try:
        response = await create_message(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)

        if "caption" not in parsed or "hashtags" not in parsed:
            raise ValueError("Missing caption or hashtags in response")

        return parsed

    except Exception:
        logger.exception("Caption generation failed for notes: %r", notes_for_caption)
        industry_tag = f"#{(ctx.industry or 'business').replace(' ', '')}"
        return {
            "caption": f"✨ {notes_for_caption}\n\nReach out to learn more!",
            "hashtags": f"{industry_tag} #smallbusiness #india #socioburp",
        }
