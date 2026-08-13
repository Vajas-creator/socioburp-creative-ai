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
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
    return _client


def _upload(key: str, data: bytes, content_type: str) -> str:
    client = _get_client()
    client.put_object(
        Bucket=settings.R2_BUCKET,
        Key=key,
        Body=io.BytesIO(data),
        ContentType=content_type,
    )
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
