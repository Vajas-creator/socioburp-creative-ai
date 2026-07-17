"""
The prompt builder is the competitive advantage described in the roadmap doc:
users never write prompts themselves. This takes a one-line brief plus the
full brand profile and produces a detailed, image-model-ready prompt.

NOTE: deliberately takes plain values (BusinessContext), not live SQLAlchemy
ORM objects — this function is always called after the DB session that
loaded the business/profile has already closed, and touching an ORM
attribute on a detached instance raises DetachedInstanceError. See
orchestrator.py where BusinessContext is built while the session is open.
"""
import json
import logging

from anthropic import AsyncAnthropic

from app.config import settings
from app.engine.context import BusinessContext

logger = logging.getLogger("socioburp.engine.prompt_builder")

client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You write prompts for an image generation model that creates
social media marketing creatives for Indian small businesses.

Given the business profile and the user's request, write ONE detailed image prompt.

Rules:
- 1080x1080 Instagram post format
- Specify: layout, headline text (short, punchy, in quotes), color scheme using
  the brand's exact hex colors if provided, visual style matching the brand
  tone, clear empty space in the bottom-right corner for logo placement
- Indian festival/cultural context when relevant (Diwali = diyas, rangoli, warm
  gold tones; Holi = color powder; Independence Day = tricolor accents;
  Raksha Bandhan = rakhi threads)
- Text on image: MAXIMUM 6-word headline + optional 4-word subline. Image
  models render long text poorly — keep it punchy.
- Offer details (discount %, dates, phone numbers) go in the CAPTION, not
  baked into the image itself.
- If brand colors are missing, pick colors appropriate to the industry and tone.
- If logo is missing, don't mention logo placement.

Reply with JSON only, no other text:
{"image_prompt": "...", "headline_text": "...", "notes_for_caption": "..."}"""


async def build(ctx: BusinessContext, user_brief: str) -> dict:
    """
    Returns {"image_prompt": str, "headline_text": str, "notes_for_caption": str}
    """
    profile_summary = _summarize_context(ctx)

    user_content = f"Business profile:\n{profile_summary}\n\nUser's request: {user_brief}"

    try:
        response = await client.messages.create(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)

        for key in ("image_prompt", "headline_text", "notes_for_caption"):
            if key not in parsed:
                raise ValueError(f"Missing key '{key}' in prompt builder output")

        return parsed

    except Exception:
        logger.exception("Prompt builder failed for brief: %r", user_brief)
        # Reasonable fallback so generation can still proceed rather than dead-end
        return {
            "image_prompt": (
                f"A clean, professional Instagram marketing post for a {ctx.industry or 'local'} "
                f"business, 1080x1080, modern design, based on this request: {user_brief}"
            ),
            "headline_text": user_brief[:40],
            "notes_for_caption": user_brief,
        }


def _summarize_context(ctx: BusinessContext) -> str:
    lines = [
        f"Business name: {ctx.name or 'Unknown'}",
        f"Industry: {ctx.industry or 'Unknown'}",
    ]
    if ctx.tone:
        lines.append(f"Brand tone: {ctx.tone}")
    if ctx.primary_color:
        lines.append(f"Primary color: {ctx.primary_color}")
    if ctx.secondary_color:
        lines.append(f"Secondary color: {ctx.secondary_color}")
    if ctx.target_audience:
        lines.append(f"Target audience: {ctx.target_audience}")
    if ctx.website:
        lines.append(f"Website: {ctx.website}")
    if ctx.contact_phone:
        lines.append(f"Contact phone: {ctx.contact_phone}")
    lines.append(f"Has logo: {'yes' if ctx.has_logo else 'no'}")
    return "\n".join(lines)
