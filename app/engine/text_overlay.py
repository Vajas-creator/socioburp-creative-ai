"""
Real, code-rendered headline text -- Aug 2026, replacing the image-gen
model's OWN attempt at painting text entirely, per the "my image and text
still cut" deep-dive. Three rounds of prompt hardening plus a hard
quality-gate cap on cropped/cut text could reduce how often the model
botched text placement, but never eliminate it: a systematic model bias
toward crowding text near the edges shows up in EVERY candidate in a
batch, not randomly in some, so "pick the best of N" has nothing good to
pick from. The categorical fix is to stop asking a diffusion model to do
something it's fundamentally unreliable at, and do it deterministically
instead -- same principle already applied to the logo (see
app/engine/logo_placement.py + compositor.py).

Two-step process, mirroring the logo's vision-based placement pattern:
  1. choose_text_box(): Claude looks at the ALREADY-FINISHED, text-free
     background and picks a rectangular region for the headline --
     genuinely empty space, not overlapping the product/subject. Fails
     safe to a fixed bottom-third box if the vision call itself fails.
  2. composite_headline(): wraps and auto-sizes the headline to fit that
     box using a REAL bundled font (app/fonts/ -- Noto Sans, covering
     Latin/Devanagari/Tamil/Telugu/Kannada/Malayalam, so non-Latin
     scripts render with actual correct glyphs instead of hoping the
     image model draws them right), with a semi-transparent scrim behind
     it so it stays legible regardless of what's actually in that region
     of the photo. This step is 100% deterministic -- there is no
     "cropped/garbled headline" failure mode left to have, by
     construction, not by better prompting.
"""
import io
import json
import logging
import os

from PIL import Image, ImageDraw, ImageFont

from app.config import settings

logger = logging.getLogger("socioburp.engine.text_overlay")

from app.anthropic_client import create_message
from app.json_extract import extract_json_text

_FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts")

# Maps app/i18n.py's language codes to (regular, bold) font filenames.
# 'en'/unrecognized/hinglish all use Latin (Hinglish is Latin-script by
# definition -- Hindi words spelled in Roman letters).
_LANGUAGE_FONT_FILES = {
    "hi": ("NotoSansDevanagari-Regular.ttf", "NotoSansDevanagari-Bold.ttf"),
    "ta": ("NotoSansTamil-Regular.ttf", "NotoSansTamil-Bold.ttf"),
    "te": ("NotoSansTelugu-Regular.ttf", "NotoSansTelugu-Bold.ttf"),
    "kn": ("NotoSansKannada-Regular.ttf", "NotoSansKannada-Bold.ttf"),
    "ml": ("NotoSansMalayalam-Regular.ttf", "NotoSansMalayalam-Bold.ttf"),
}
_DEFAULT_FONT_FILES = ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf")

MARGIN = 24

CHOOSE_BOX_SYSTEM_PROMPT = """You are laying out a headline for a
marketing creative. You'll see the finished background photo/design
(with NO text on it yet) and the exact headline text that needs to go
somewhere on it.

Pick a rectangular region (x, y = top-left corner, width, height) for the
headline that:
- Sits on relatively plain/uncluttered background, not covering the
  main product/subject or any other important visual element.
- Is wide and tall enough to comfortably fit the given text at a large,
  confident, easily-readable size (err on the generous side -- a bit
  more room is better than text that has to shrink tiny to fit).
- Makes sense compositionally (usually the lower third or upper third of
  a portrait image, but use real judgment about this specific image).

Reply with JSON only, no other text: {"x": <int>, "y": <int>, "width": <int>, "height": <int>}"""


def _font_files_for_language(language: str | None) -> tuple[str, str]:
    return _LANGUAGE_FONT_FILES.get(language or "en", _DEFAULT_FONT_FILES)


async def choose_text_box(image_bytes: bytes, image_w: int, image_h: int, headline: str) -> tuple[int, int, int, int]:
    """
    Returns (x, y, width, height), clamped to stay fully within the
    canvas. Falls back to a fixed bottom-third box on any failure -- see
    module docstring; this is a placement-QUALITY concern, not a safety
    one (unlike the old crop-based approach, there's no way for this
    fallback to result in cut-off text, only a less inspired layout).
    """
    import base64

    fallback = (MARGIN, int(image_h * 0.62), image_w - 2 * MARGIN, int(image_h * 0.3))

    try:
        response = await create_message(
            model=settings.CLAUDE_INTENT_MODEL,
            max_tokens=80,
            system=CHOOSE_BOX_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Image size: {image_w}x{image_h}. Headline text: {headline!r}"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(image_bytes).decode("utf-8")}},
                ],
            }],
        )
        text = extract_json_text(response.content[0].text.strip())
        parsed = json.loads(text)

        x = max(MARGIN, min(int(parsed["x"]), image_w - MARGIN))
        y = max(MARGIN, min(int(parsed["y"]), image_h - MARGIN))
        w = max(50, min(int(parsed["width"]), image_w - x - MARGIN))
        h = max(30, min(int(parsed["height"]), image_h - y - MARGIN))
        return (x, y, w, h)

    except Exception:
        logger.exception("Text box placement vision call failed — falling back to a fixed bottom-third box")
        return fallback


def _wrap_and_fit(draw: ImageDraw.ImageDraw, text: str, font_path: str, box_w: int, box_h: int) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """
    Finds the largest font size (within a sane range) at which `text`,
    word-wrapped to fit box_w, also fits within box_h. Always returns
    SOMETHING drawable, even if it has to shrink to the minimum size --
    there is no failure path here that results in text being cut off,
    only progressively smaller text.
    """
    for size in range(96, 15, -4):
        font = ImageFont.truetype(font_path, size)
        words = text.split()
        lines, current = [], ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=font) <= box_w:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)

        line_height = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
        total_height = line_height * len(lines) * 1.25
        widest_line = max((draw.textlength(line, font=font) for line in lines), default=0)

        if total_height <= box_h and widest_line <= box_w:
            return font, lines

    # Smallest size didn't fit either -- use it anyway, best-effort wrap.
    font = ImageFont.truetype(font_path, 16)
    return font, [text]


async def composite_headline(image_bytes: bytes, headline: str, language: str | None = None) -> bytes:
    """
    Composites `headline` onto the image as real, crisp text with a
    semi-transparent scrim behind it for guaranteed legibility. Returns
    PNG bytes. If anything goes wrong, returns the original image
    unmodified rather than failing the whole generation -- same
    fail-safe pattern as compositor.py's composite_logo().
    """
    if not headline or not headline.strip():
        return image_bytes

    try:
        base = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        box = await choose_text_box(image_bytes, base.width, base.height, headline)
        x, y, w, h = box

        _, bold_file = _font_files_for_language(language)
        font_path = os.path.join(_FONTS_DIR, bold_file)

        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Leave a little internal padding within the chosen box so the
        # scrim doesn't hug the text too tightly.
        pad = 20
        font, lines = _wrap_and_fit(draw, headline.strip(), font_path, max(10, w - 2 * pad), max(10, h - 2 * pad))

        line_height = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
        line_spacing = int(line_height * 1.25)
        text_block_height = line_spacing * len(lines)
        widest_line = max((draw.textlength(line, font=font) for line in lines), default=0)

        scrim_box = (
            x, y + (h - text_block_height) // 2 - pad,
            x + w, y + (h - text_block_height) // 2 + text_block_height + pad,
        )
        draw.rectangle(scrim_box, fill=(0, 0, 0, 115))

        text_y = scrim_box[1] + pad
        for line in lines:
            line_w = draw.textlength(line, font=font)
            text_x = x + (w - line_w) // 2
            draw.text((text_x, text_y), line, font=font, fill=(255, 255, 255, 255))
            text_y += line_spacing

        composited = Image.alpha_composite(base, overlay)
        out = io.BytesIO()
        composited.convert("RGB").save(out, format="PNG")
        return out.getvalue()

    except Exception:
        logger.exception("Headline text compositing failed — returning image without headline text")
        return image_bytes
