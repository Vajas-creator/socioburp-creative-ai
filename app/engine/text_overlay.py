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

CHOOSE_BOX_SYSTEM_PROMPT = """You are laying out text for a marketing
creative. You'll see the finished background photo/design (with NO text
on it yet) and the exact text content that needs to go somewhere on it --
this may be just a headline, or a headline plus one or two smaller
supporting lines (a subtext line, a CTA/website/contact line) stacked
underneath it.

Pick a rectangular region (x, y = top-left corner, width, height) that:
- Sits on relatively plain/uncluttered background, not covering the
  main product/subject or any other important visual element.
- Is wide and tall enough to comfortably fit ALL of the given text
  lines stacked vertically, the headline at a large confident size and
  any supporting lines beneath it at smaller sizes (err on the generous
  side -- a bit more room is better than text that has to shrink tiny to
  fit; more lines of text need a taller box, not just a wider one).
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


def _wrap_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, box_w: int) -> list[str]:
    """Greedy word-wrap of `text` to fit `box_w` at the given font. Never drops a word."""
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
    return lines


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
        lines = _wrap_lines(draw, text, font, box_w)

        line_height = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
        total_height = line_height * len(lines) * 1.25
        widest_line = max((draw.textlength(line, font=font) for line in lines), default=0)

        if total_height <= box_h and widest_line <= box_w:
            return font, lines

    # Smallest size didn't fit either -- use it anyway, best-effort wrap.
    font = ImageFont.truetype(font_path, 16)
    return font, [text]


# Aug 2026 "multiple text elements per slide" follow-up: a real brief asked
# for a bold headline PLUS a smaller subtext line PLUS a small CTA/website
# line, all on the same image -- composite_headline() below only ever
# rendered a single headline, so the rest of that text was silently
# dropped (worse: the image model was explicitly forbidden from painting
# ANY text per prompt_builder.py's "NO TEXT" rule, so the extra content
# just vanished with no error). _fit_text_blocks() extends the same
# never-drop-text, shrink-to-fit philosophy to up to three stacked blocks
# of decreasing visual weight instead of just one.
_SUBTEXT_SIZE_RATIO = 0.45
_CTA_SIZE_RATIO = 0.32
_MIN_SUBTEXT_SIZE = 14
_MIN_CTA_SIZE = 12
_BLOCK_GAP_RATIO = 0.4  # vertical gap after a block, relative to that block's own font size


def _fit_text_blocks(
    draw: ImageDraw.ImageDraw,
    headline: str,
    subtext: str | None,
    cta_text: str | None,
    bold_font_path: str,
    regular_font_path: str,
    box_w: int,
    box_h: int,
) -> list[dict]:
    """
    Like _wrap_and_fit(), but for up to three stacked blocks (headline,
    optional subtext, optional cta) at once: headline in bold at the
    largest size, subtext/cta in regular weight at proportionally smaller
    sizes (subtext ~45% of headline size, cta ~32%), all shrunk together
    until the whole stack fits box_h, or the size floor is hit -- same
    guarantee as _wrap_and_fit: text is never dropped, only shrunk.

    Returns a list of dicts: {"lines": [...], "font": ImageFont, "color":
    (r,g,b,a), "line_spacing": int, "block_height": int, "block_gap": int}
    in top-to-bottom drawing order.
    """
    specs = [("headline", headline, bold_font_path, (255, 255, 255, 255))]
    if subtext and subtext.strip():
        specs.append(("subtext", subtext.strip(), regular_font_path, (255, 255, 255, 235)))
    if cta_text and cta_text.strip():
        specs.append(("cta", cta_text.strip(), regular_font_path, (255, 255, 255, 200)))

    last_attempt = None
    for headline_size in range(80, 15, -4):
        sizes = {
            "headline": headline_size,
            "subtext": max(_MIN_SUBTEXT_SIZE, round(headline_size * _SUBTEXT_SIZE_RATIO)),
            "cta": max(_MIN_CTA_SIZE, round(headline_size * _CTA_SIZE_RATIO)),
        }

        blocks = []
        total_height = 0
        fits = True
        for name, text, font_path, color in specs:
            size = sizes[name]
            font = ImageFont.truetype(font_path, size)
            lines = _wrap_lines(draw, text, font, box_w)
            line_height = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
            line_spacing = int(line_height * 1.2)
            block_height = line_spacing * len(lines)
            gap = int(size * _BLOCK_GAP_RATIO)
            widest = max((draw.textlength(line, font=font) for line in lines), default=0)
            if widest > box_w:
                fits = False
            blocks.append({
                "lines": lines, "font": font, "color": color,
                "line_spacing": line_spacing, "block_height": block_height, "block_gap": gap,
            })
            total_height += block_height + gap

        total_height -= blocks[-1]["block_gap"]  # no trailing gap after the last block
        last_attempt = blocks
        if fits and total_height <= box_h:
            return blocks

    # Nothing fit even at the size floor -- use the smallest attempt
    # anyway, best-effort. Never silently drop a block.
    return last_attempt


async def composite_headline(
    image_bytes: bytes,
    headline: str,
    subtext: str | None = None,
    cta_text: str | None = None,
    language: str | None = None,
) -> bytes:
    """
    Composites `headline` -- and, if given, a smaller `subtext` line and an
    even smaller `cta_text` line stacked beneath it -- onto the image as
    real, crisp text with a semi-transparent scrim behind the whole stack
    for guaranteed legibility. Returns PNG bytes. If anything goes wrong,
    returns the original image unmodified rather than failing the whole
    generation -- same fail-safe pattern as compositor.py's
    composite_logo().

    subtext/cta_text are optional (Aug 2026 "headline + subtext + CTA"
    follow-up) -- a request that only needs a single headline behaves
    exactly as before.
    """
    if not headline or not headline.strip():
        return image_bytes

    try:
        base = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        combined_text = "\n".join(t.strip() for t in (headline, subtext, cta_text) if t and t.strip())
        box = await choose_text_box(image_bytes, base.width, base.height, combined_text)
        x, y, w, h = box

        reg_file, bold_file = _font_files_for_language(language)
        bold_path = os.path.join(_FONTS_DIR, bold_file)
        reg_path = os.path.join(_FONTS_DIR, reg_file)

        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Leave a little internal padding within the chosen box so the
        # scrim doesn't hug the text too tightly.
        pad = 20
        blocks = _fit_text_blocks(
            draw, headline.strip(), subtext, cta_text, bold_path, reg_path,
            max(10, w - 2 * pad), max(10, h - 2 * pad),
        )
        total_height = sum(b["block_height"] + b["block_gap"] for b in blocks) - blocks[-1]["block_gap"]

        scrim_box = (
            x, y + (h - total_height) // 2 - pad,
            x + w, y + (h - total_height) // 2 + total_height + pad,
        )
        draw.rectangle(scrim_box, fill=(0, 0, 0, 115))

        text_y = scrim_box[1] + pad
        for block in blocks:
            for line in block["lines"]:
                line_w = draw.textlength(line, font=block["font"])
                text_x = x + (w - line_w) // 2
                draw.text((text_x, text_y), line, font=block["font"], fill=block["color"])
                text_y += block["line_spacing"]
            text_y += block["block_gap"]

        composited = Image.alpha_composite(base, overlay)
        out = io.BytesIO()
        composited.convert("RGB").save(out, format="PNG")
        return out.getvalue()

    except Exception:
        logger.exception("Headline text compositing failed — returning image without headline text")
        return image_bytes
