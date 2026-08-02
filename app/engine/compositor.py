"""
Logo compositing. Deliberately dumb for MVP per the build guide: a plain
Pillow paste at a chosen corner (or center), auto-scaled. No background
segmentation, no AI-assisted placement. Revisit only if pilot feedback
specifically flags it.
"""
import io
import logging

from PIL import Image

logger = logging.getLogger("socioburp.engine.compositor")

DEFAULT_POSITION = "bottom-right"


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


def composite_logo(creative_bytes: bytes, logo_bytes: bytes, position: str = DEFAULT_POSITION) -> bytes:
    """
    Pastes the logo at the named position (top-left / top-right / bottom-left /
    bottom-right / center) with a margin, scaled to ~12% of the creative's
    width. Returns PNG bytes. If anything goes wrong, returns the original
    creative unmodified rather than failing the whole generation.
    """
    try:
        base = Image.open(io.BytesIO(creative_bytes)).convert("RGBA")
        logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")

        target_w = int(base.width * 0.12)
        scale = target_w / logo.width
        logo = logo.resize((target_w, int(logo.height * scale)))

        margin = 24
        coords = _position_coords(position, base.width, base.height, logo.width, logo.height, margin)
        base.paste(logo, coords, logo)

        out = io.BytesIO()
        base.convert("RGB").save(out, format="PNG")
        return out.getvalue()

    except Exception:
        logger.exception("Logo compositing failed — returning creative without logo.")
        return creative_bytes
