"""
Aug 2026 "why is my logo merging with a visible white box" fix: a client's
logo uploaded on a plain white (or any other solid-color) background
arrives with NO real transparency to composite against -- WhatsApp
re-encodes uploaded images as JPEG, which has no alpha channel at all, so
compositor.py's `.convert("RGBA")` gave every pixel full opacity. The
result: the logo's entire bounding rectangle, including its background
color, got pasted as a visible solid block on top of the creative,
instead of just the logo mark blending onto whatever's actually behind
it.

remove_uniform_background() runs ONCE, at upload time (see
app/engine/logo_capture.py), so every future composite -- smart or
fixed-position -- automatically gets a clean, transparent logo with no
per-generation reprocessing needed.

Deliberately a plain flood-fill, not an ML background-removal model: this
codebase avoids heavy ML dependencies where a deterministic approach
already handles the actual real-world case (a logo exported or
screenshotted on a flat white or brand-color background, by far the
common case for a small business's logo file) -- same principle already
applied to text (app/engine/text_overlay.py) and canvas resizing
(app/engine/image_gen.py's outpaint fix): do the mechanical, reliable
thing in code instead of reaching for a probabilistic model where a
deterministic answer exists.
"""
import io
import logging

from PIL import Image, ImageDraw

logger = logging.getLogger("socioburp.engine.logo_bg_removal")

_CORNER_TOLERANCE = 24
_FLOODFILL_TOLERANCE = 24
_MIN_REMOVED_FRACTION = 0.03  # below this, treat as "no real background found"
_MAX_REMOVED_FRACTION = 0.92  # above this, this erased the actual logo mark, not just its background -- bail out


def _colors_close(a: tuple, b: tuple, tol: int) -> bool:
    return all(abs(a[i] - b[i]) <= tol for i in range(3))


def remove_uniform_background(image_bytes: bytes) -> bytes:
    """
    If `image_bytes` has a roughly uniform-colored background reachable
    from its edges (the common case for a logo exported on white, or any
    other flat color), keys that color out to transparency via a flood
    fill seeded from the border and edge midpoints. Returns PNG bytes
    either way -- unmodified content (just re-encoded as PNG) if no
    uniform background is detected, or if removing it would erase almost
    the whole image (a safety net against a genuinely solid-color logo
    mark with no real background to remove, so this never makes a logo
    WORSE than leaving it alone).
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        w, h = img.size
        if w < 4 or h < 4:
            return _to_png(img)

        corners = [img.getpixel((0, 0)), img.getpixel((w - 1, 0)), img.getpixel((0, h - 1)), img.getpixel((w - 1, h - 1))]
        if not all(_colors_close(corners[0], c, _CORNER_TOLERANCE) for c in corners[1:]):
            # Corners disagree -- not a simple uniform background (could
            # already be a properly-transparent PNG, or a busy/photo-style
            # logo). Leave it untouched rather than guessing.
            logger.info("Logo corners aren't a uniform color — leaving background untouched")
            return _to_png(img)

        seeds = [
            (0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
            (w // 2, 0), (0, h // 2), (w - 1, h // 2), (w // 2, h - 1),
        ]
        for seed in seeds:
            if img.getpixel(seed)[3] == 0:
                continue  # already made transparent by an earlier seed
            ImageDraw.floodfill(img, seed, (0, 0, 0, 0), thresh=_FLOODFILL_TOLERANCE)

        removed_fraction = img.getchannel("A").histogram()[0] / (w * h)

        if removed_fraction < _MIN_REMOVED_FRACTION or removed_fraction > _MAX_REMOVED_FRACTION:
            logger.info(
                "Background removal skipped (removed_fraction=%.3f outside [%.2f, %.2f]) — using original image",
                removed_fraction, _MIN_REMOVED_FRACTION, _MAX_REMOVED_FRACTION,
            )
            return _to_png(Image.open(io.BytesIO(image_bytes)).convert("RGBA"))

        return _to_png(img)

    except Exception:
        logger.exception("Logo background removal failed — using the original image unmodified")
        return image_bytes


def _to_png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
