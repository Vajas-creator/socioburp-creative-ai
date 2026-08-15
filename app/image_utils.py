"""
Small, shared image-format helpers used by more than one engine module --
pulled out of app/engine/agent.py (where it first appeared, for the
image/jpeg-vs-image/png Claude vision crash fix) once app/engine/
logo_capture.py needed the exact same media-type sniffing for color
extraction from an uploaded logo. Same rationale as app/json_extract.py:
one real implementation, not a second copy-paste.
"""
import io
import logging

from PIL import Image

logger = logging.getLogger("socioburp.image_utils")

_PIL_FORMAT_TO_MEDIA_TYPE = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}


def detect_image_media_type(image_bytes: bytes) -> str:
    """Sniffs the real format from the bytes -- WhatsApp media isn't reliably any one format, so this can't be hardcoded. Defaults to jpeg (WhatsApp's typical format) if detection itself fails, rather than raising."""
    try:
        fmt = Image.open(io.BytesIO(image_bytes)).format
        return _PIL_FORMAT_TO_MEDIA_TYPE.get(fmt, "image/jpeg")
    except Exception:
        logger.exception("Failed to detect image format — defaulting to image/jpeg")
        return "image/jpeg"
