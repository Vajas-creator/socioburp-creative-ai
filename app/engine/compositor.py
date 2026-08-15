"""
Logo compositing. Was "deliberately dumb for MVP" -- a plain Pillow paste
at one of 5 fixed named corners, auto-scaled, no AI-assisted placement --
per the original build guide. Revisited per the Aug 2026 "I want the
engine to be smart... not template king of a thing" feedback: composite_logo()
now has a `smart=True` mode that has app/engine/logo_placement.py actually
LOOK at the finished creative and choose real coordinates, instead of
always landing in one of 5 rigid slots.

The fixed named-position path (smart=False, the default) is kept
unchanged and still used by orchestrator.py's _recomposite_logo() free
"move my logo" revision fast path -- there, the client gave an EXPLICIT
literal position command ("top left"), so honoring it directly is
correct; that's a real instruction to follow, not a case needing
open-ended reasoning about where looks best.
"""
import io
import logging

from PIL import Image

logger = logging.getLogger("socioburp.engine.compositor")

DEFAULT_POSITION = "bottom-right"
MARGIN = 24


def _position_coords(position: str, base_w: int, base_h: int, logo_w: int, logo_h: int, margin: int) -> tuple:
    """Maps a named position to paste coordinates. Unknown names fall back to bottom-right."""
    coords = {
        "top-left": (margin, margin),
        "top-right": (base_w - logo_w - margin, margin),
        "bottom-left": (margin, base_h - logo_h - margin),
        "bottom-right": (base_w - logo_w - margin, base_h - logo_h - margin),
        "center": ((base_w - logo_w) // 2, (base_h - logo_h) // 2),
    }
    if position not in coords:
        logger.warning("Unknown logo position %r — falling back to %s", position, DEFAULT_POSITION)
    return coords.get(position, coords[DEFAULT_POSITION])


def _rects_overlap(r1: tuple, r2: tuple) -> bool:
    x1, y1, w1, h1 = r1
    x2, y2, w2, h2 = r2
    return not (x1 + w1 <= x2 or x2 + w2 <= x1 or y1 + h1 <= y2 or y2 + h2 <= y1)


def _avoid_overlap(coords: tuple, logo_w: int, logo_h: int, base_w: int, base_h: int, avoid_rect: tuple | None, margin: int) -> tuple:
    """
    Aug 2026 "logo overlapping my text" fix: logo_placement.py's vision
    call is told to avoid the headline text, and usually does, but a
    vision model estimating pixel coordinates isn't guaranteed precise --
    a close-but-wrong guess can still visibly clip the text scrim. Rather
    than trust that estimate alone, this checks the ACTUAL chosen spot
    against the ACTUAL drawn text rectangle (real pixel math, not a
    guess) and deterministically substitutes a non-overlapping named
    corner if it collides -- same "never trust probabilistic precision
    when exact math can guarantee it" principle as image_gen.py's outpaint
    fix and text_overlay.py's own font rendering.

    Tries corners in a fixed priority order and returns the first one
    that clears `avoid_rect` (and stays on-canvas). If literally none do
    (e.g. a scrim spanning nearly the whole canvas), returns the original
    `coords` unchanged -- no worse than not having this check at all.
    """
    if avoid_rect is None:
        return coords

    logo_rect = (coords[0], coords[1], logo_w, logo_h)
    if not _rects_overlap(logo_rect, avoid_rect):
        return coords

    max_x = max(margin, base_w - logo_w - margin)
    max_y = max(margin, base_h - logo_h - margin)
    candidates = [
        (max_x, max_y),                      # bottom-right
        (max_x, margin),                     # top-right
        (margin, max_y),                     # bottom-left
        (margin, margin),                    # top-left
        ((base_w - logo_w) // 2, margin),    # top-center
    ]
    for cand in candidates:
        if not _rects_overlap((cand[0], cand[1], logo_w, logo_h), avoid_rect):
            logger.info("Logo placement %s overlapped the text area — moved to %s instead", coords, cand)
            return cand

    logger.warning("No candidate logo position clears the text area (avoid_rect=%s) — keeping original choice", avoid_rect)
    return coords


async def composite_logo(
    creative_bytes: bytes, logo_bytes: bytes, position: str = DEFAULT_POSITION,
    smart: bool = False, preference: str | None = None, avoid_rect: tuple | None = None,
) -> bytes:
    """
    Pastes the logo (scaled to ~12% of the creative's width) onto the
    creative. Returns PNG bytes. If anything goes wrong, returns the
    original creative unmodified rather than failing the whole generation.

    smart=False (default): named position (top-left / top-right /
    bottom-left / bottom-right / center) with a fixed margin -- unchanged
    original behavior, used for an explicit literal position command.

    smart=True: ignores `position` and instead calls
    app/engine/logo_placement.py to choose real coordinates by actually
    looking at this specific image -- genuinely empty space, not
    overlapping text/product, honoring `preference` (free-form text, e.g.
    BrandProfile.extras["logo_position_hint"]) as a soft directive for
    which general area. Falls back to the named-position default if the
    vision call itself fails for any reason -- same fail-safe pattern as
    everywhere else in this codebase.

    avoid_rect: (x, y, width, height) of the actual composited headline/
    subtext/CTA text area, if known (see text_overlay.composite_headline()'s
    return value) -- Aug 2026 follow-up. Whatever position gets chosen
    (smart or named) is checked against this rect and deterministically
    substituted for a clear corner if it overlaps, rather than relying
    solely on the vision call's own precision to avoid it.
    """
    try:
        base = Image.open(io.BytesIO(creative_bytes)).convert("RGBA")
        logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")

        target_w = int(base.width * 0.12)
        scale = target_w / logo.width
        logo = logo.resize((target_w, int(logo.height * scale)))

        coords = None
        if smart:
            from app.engine import logo_placement
            base_rgb_bytes = io.BytesIO()
            base.convert("RGB").save(base_rgb_bytes, format="PNG")
            coords = await logo_placement.choose_position(
                base_rgb_bytes.getvalue(), base.width, base.height, logo.width, logo.height, preference,
            )

        if coords is None:
            coords = _position_coords(position, base.width, base.height, logo.width, logo.height, MARGIN)
        else:
            # Belt-and-suspenders: logo_placement.py already clamps, but
            # this function must never be the reason a logo ends up cut
            # off, regardless of what any caller passes in.
            max_x = max(MARGIN, base.width - logo.width - MARGIN)
            max_y = max(MARGIN, base.height - logo.height - MARGIN)
            coords = (max(MARGIN, min(coords[0], max_x)), max(MARGIN, min(coords[1], max_y)))

        coords = _avoid_overlap(coords, logo.width, logo.height, base.width, base.height, avoid_rect, MARGIN)

        base.paste(logo, coords, logo)

        out = io.BytesIO()
        base.convert("RGB").save(out, format="PNG")
        return out.getvalue()

    except Exception:
        logger.exception("Logo compositing failed — returning creative without logo.")
        return creative_bytes
