"""
Screenshot-based brand color discovery — replaces asking for a raw hex
code (bad UX for a non-technical small business owner who doesn't know
their hex code off-hand) with: send a screenshot of your Instagram (or
logo), Claude's vision extracts the dominant brand colors, the client
confirms or corrects.

Deliberately conservative: this SUGGESTS colors for explicit confirmation
— it never silently applies them. Getting a client's brand colors wrong
is a trust problem (their own audience notices immediately), so unlike
language auto-detection (low-stakes, auto-apply-then-allow-override), this
stays a propose-then-confirm step. See app/onboarding.py's
awaiting_color_confirm state.
"""
import base64
import json
import logging

from anthropic import AsyncAnthropic

from app.config import settings

logger = logging.getLogger("socioburp.engine.color_discovery")

client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

COLOR_EXTRACTION_SYSTEM_PROMPT = """You are analyzing an image (a small business's
Instagram screenshot and/or logo) to identify their brand's primary color palette,
for use in future marketing creatives.

Identify the 1-2 most dominant, INTENTIONAL brand colors — not incidental colors
(skin tones, food, sky, random background objects), but colors that appear
deliberately used for branding: logo colors, a consistent accent color across
posts, a graphic design background color. If the image is a logo, its own colors
ARE the brand colors. If it's an Instagram grid screenshot, look for what repeats
across multiple posts, not just one photo's colors.

If you genuinely can't identify confident brand colors (e.g. it's just a photo of
food or a person with no clear brand color signal), say so honestly rather than
guessing at photo colors.

Reply with JSON only, no other text:
{"primary_color": "#RRGGBB", "secondary_color": "#RRGGBB or null", "confident": true or false}"""


async def extract_colors_from_image(image_bytes: bytes, media_type: str = "image/jpeg") -> dict | None:
    """
    Returns {"primary_color": str, "secondary_color": str|None, "confident": bool}
    or None on any failure (fails safe — caller falls back to the manual
    hex-code question, same as if the client had typed 'skip').
    """
    try:
        b64_data = base64.b64encode(image_bytes).decode("utf-8")
        response = await client.messages.create(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=200,
            system=COLOR_EXTRACTION_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64_data}},
                    {"type": "text", "text": "Extract this business's brand colors."},
                ],
            }],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)

        if "primary_color" not in parsed:
            raise ValueError("Missing primary_color in response")

        return parsed

    except Exception:
        logger.exception("Color extraction from image failed")
        return None
