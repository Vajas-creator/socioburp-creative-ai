"""
Logo compositing. Deliberately dumb for MVP per the build guide: a plain
Pillow paste at bottom-right, auto-scaled. No background segmentation, no
AI-assisted placement. Revisit only if pilot feedback specifically flags it.
"""
import io
import logging

from PIL import Image

logger = logging.getLogger("socioburp.engine.compositor")


def composite_logo(creative_bytes: bytes, logo_bytes: bytes) -> bytes:
    """
    Pastes the logo at bottom-right with a margin, scaled to ~12% of the
    creative's width. Returns PNG bytes. If anything goes wrong, returns
    the original creative unmodified rather than failing the whole generation.
    """
    try:
        base = Image.open(io.BytesIO(creative_bytes)).convert("RGBA")
        logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")

        target_w = int(base.width * 0.12)
        scale = target_w / logo.width
        logo = logo.resize((target_w, int(logo.height * scale)))

        margin = 24
        position = (base.width - target_w - margin, base.height - logo.height - margin)
        base.paste(logo, position, logo)

        out = io.BytesIO()
        base.convert("RGB").save(out, format="PNG")
        return out.getvalue()

    except Exception:
        logger.exception("Logo compositing failed — returning creative without logo.")
        return creative_bytes
