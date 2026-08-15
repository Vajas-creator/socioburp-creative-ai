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


async def composite_logo(
    creative_bytes: bytes, logo_bytes: bytes, position: str = DEFAULT_POSITION,
    smart: bool = False, preference: str | None = None,
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

        base.paste(logo, coords, logo)

        out = io.BytesIO()
        base.convert("RGB").save(out, format="PNG")
        return out.getvalue()

    except Exception:
        logger.exception("Logo compositing failed — returning creative without logo.")
        return creative_bytes
