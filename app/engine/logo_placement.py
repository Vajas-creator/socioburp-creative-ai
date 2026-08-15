"""
Vision-based logo placement -- Aug 2026 "I want the engine to be smart...
not template king of a thing" feedback. Previously (and still, for the
explicit "move my logo" revision fast path -- see _recomposite_logo in
orchestrator.py) logo placement was a plain lookup into 5 fixed named
slots (see app/engine/compositor.py's _position_coords()). This instead
has Claude actually LOOK at the finished creative and choose real pixel
coordinates -- genuinely empty space, not overlapping text or the
product/subject -- honoring any client-stated preference
(BrandProfile.extras["logo_position_hint"], free text, not a fixed enum)
as a soft directive for which general area, not a literal instruction to
follow blindly if the literal spot isn't actually clear.

Coordinates returned are ALWAYS clamped here to stay fully within the
canvas, and clamped AGAIN by the caller (compositor.composite_logo()) --
the model choosing a mediocre spot is a quality problem to live with, the
logo actually going off-canvas is not something to ever risk on a model
response alone.
"""
import base64
import json
import logging

from app.config import settings

logger = logging.getLogger("socioburp.engine.logo_placement")

from app.anthropic_client import create_message
from app.json_extract import extract_json_text

MARGIN = 24

SYSTEM_PROMPT = """You are placing a business's logo onto a finished
marketing creative image. You'll see the creative and be told the logo's
size (already scaled to fit it).

Pick the (x, y) pixel coordinates for the logo's TOP-LEFT corner such
that it lands on genuinely empty/plain background -- never overlapping
the headline text, any other on-image text, or the product/subject.

If a client placement preference is given, treat it as which general
AREA of the image they'd like it in (e.g. "middle", "top", "in the
corner", "somewhere subtle", "next to the offer text") -- honor that
intent, but still choose the specific empty spot within/near that area
yourself; don't ignore a stated preference, but don't follow it so
literally that the logo ends up overlapping something if the exact
literal spot isn't actually clear in this particular image.

If there's no stated preference, or the image genuinely doesn't support
it, just pick whatever empty space looks best on its own merits -- usually
a corner, but not always; use real judgment about this specific image
rather than defaulting to the same spot every time.

Reply with JSON only, no other text: {"x": <int>, "y": <int>}"""


async def choose_position(
    image_bytes: bytes, image_w: int, image_h: int, logo_w: int, logo_h: int, preference: str | None,
) -> tuple[int, int] | None:
    """
    Returns (x, y) for the logo's top-left corner, clamped to stay fully
    on-canvas, or None on any failure -- callers should fall back to
    their own default (compositor.py falls back to the named-position
    bottom-right default) in that case.
    """
    max_x = max(MARGIN, image_w - logo_w - MARGIN)
    max_y = max(MARGIN, image_h - logo_h - MARGIN)

    try:
        user_text = (
            f"Creative image size: {image_w}x{image_h} pixels. "
            f"Logo size (already scaled to fit): {logo_w}x{logo_h} pixels. "
            f"Client's stated placement preference: {preference or '(none given -- use your own judgment)'}"
        )
        response = await create_message(
            model=settings.CLAUDE_INTENT_MODEL,
            max_tokens=60,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(image_bytes).decode("utf-8")},
                    },
                ],
            }],
        )
        text = response.content[0].text.strip()
        text = extract_json_text(text)
        parsed = json.loads(text)

        x = max(MARGIN, min(int(parsed["x"]), max_x))
        y = max(MARGIN, min(int(parsed["y"]), max_y))
        return (x, y)

    except Exception:
        logger.exception("Logo placement vision call failed — caller should fall back to a default position")
        return None
