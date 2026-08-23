"""
Cloudflare R2 storage (S3-compatible API). Used for logos (uploaded during
onboarding) and generated creatives (uploaded after image generation).

R2 setup (one-time, outside this code):
  1. Cloudflare dashboard -> R2 -> Create bucket -> name it e.g. "socioburp-creatives"
  2. R2 -> Manage API tokens -> Create API token -> permissions: Object Read & Write
     -> copy Account ID, Access Key ID, Secret Access Key
  3. Bucket -> Settings -> enable public access (or connect a custom domain,
     e.g. cdn.socioburp.net) so WhatsApp can fetch the image URL directly.
  4. Set env vars: R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET,
     R2_PUBLIC_BASE_URL (e.g. https://cdn.socioburp.net or the r2.dev URL)
"""
import io
import logging
import uuid

import boto3
from botocore.client import Config

from app.config import settings

logger = logging.getLogger("socioburp.storage")

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.R2_ACCESS_KEY,
            aws_secret_access_key=settings.R2_SECRET_KEY,
            # Aug 2026 "bot silent after 'give me a moment'" root cause: this
            # Config previously set NOTHING about timeouts, so every upload
            # ran on botocore's own defaults -- 60s connect + 60s read, PER
            # RETRY ATTEMPT (botocore retries connection-level failures on
            # its own, on top of anything this app does). A transient R2
            # network hiccup could silently block a generation for several
            # minutes with zero log output and zero message to the client,
            # since Python can't reach any except block while still awaiting
            # inside orchestrator._run_generation()'s try -- a prior fix
            # (routing reflect_first_result() through that same try/except)
            # correctly handled a call that RAISES quickly, but did nothing
            # for a call that just hangs, which is what this actually was.
            # Short explicit timeouts + a small bounded retry count turn
            # a real R2 problem into a FAST, LOUD failure instead -- it
            # still flows into _run_generation()'s existing exception
            # handler (alert + "Something went wrong" to the client), it
            # just gets there in ~15-20s instead of several minutes of
            # silence.
            config=Config(
                signature_version="s3v4",
                connect_timeout=10,
                read_timeout=20,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
            region_name="auto",
        )
    return _client


def _upload(key: str, data: bytes, content_type: str) -> str:
    client = _get_client()
    try:
        client.put_object(
            Bucket=settings.R2_BUCKET,
            Key=key,
            Body=io.BytesIO(data),
            ContentType=content_type,
        )
    except Exception:
        # Explicit, specifically-labeled log line (Aug 2026 "silent
        # failure" investigation) -- the caller's own exception handling
        # (orchestrator.py's try/except around the whole generation) still
        # catches this and notifies the client; this just makes an R2
        # failure immediately identifiable in logs instead of looking like
        # a generic, unlabeled crash somewhere in the pipeline.
        logger.exception("R2 upload failed for key=%s", key)
        raise
    url = f"{settings.R2_PUBLIC_BASE_URL.rstrip('/')}/{key}"
    logger.info("Uploaded to R2: %s", key)
    return url


def upload_logo(business_id: uuid.UUID, image_bytes: bytes) -> str:
    key = f"logos/{business_id}.png"
    return _upload(key, image_bytes, "image/png")


def upload_creative(business_id: uuid.UUID, generation_id: uuid.UUID, image_bytes: bytes) -> str:
    key = f"creatives/{business_id}/{generation_id}.png"
    return _upload(key, image_bytes, "image/png")


def upload_base_image(business_id: uuid.UUID, generation_id: uuid.UUID, image_bytes: bytes) -> str:
    """
    The pre-composite background (creative WITHOUT the logo pasted on).
    Stored alongside the final creative so a later "move my logo" revision
    can re-paste at a new position without regenerating the image.
    """
    key = f"creatives/{business_id}/{generation_id}_base.png"
    return _upload(key, image_bytes, "image/png")


def upload_carousel_slide(business_id: uuid.UUID, generation_id: uuid.UUID, slide_num: int, image_bytes: bytes) -> str:
    """One slide (1-indexed) of a carousel generation -- see Generation.carousel_image_urls."""
    key = f"creatives/{business_id}/{generation_id}_slide{slide_num}.png"
    return _upload(key, image_bytes, "image/png")


def upload_reference_image(business_id: uuid.UUID, image_bytes: bytes) -> str:
    """
    A client's uploaded photo, persisted immediately so it survives a
    multi-turn negotiation (carousel slide-count/content questions, or
    "what would you like me to do with this image") without depending on
    WhatsApp's media_id staying resolvable for however long that takes --
    see app/engine/carousel.py and app/engine/image_intent.py.
    """
    key = f"references/{business_id}/{uuid.uuid4()}.png"
    return _upload(key, image_bytes, "image/png")
