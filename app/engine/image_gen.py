"""
Image generation. Deliberately abstracted behind one function (`generate_images`)
so that once you run the 20-prompt benchmark (see build guide, Week 2 Day 10-11)
you can lock in the winning provider by just changing IMAGE_PROVIDER in env vars
— no other code changes needed.

Generates 2 candidates per request (cost compromise for MVP) — the quality
checker in quality.py picks the better one.

NOTE: Only the OpenAI provider is implemented below. Add Ideogram/FLUX
branches here once you've run the benchmark and know which one(s) you need.
Verify the exact current API request/response shape against the provider's
live docs before your first real run — image-gen APIs change fairly often.
"""
import asyncio
import base64
import io
import logging

import httpx
from PIL import Image

from app.config import settings

logger = logging.getLogger("socioburp.engine.image_gen")

# Every generated creative must ship at exactly this size (~4:5 portrait),
# regardless of what the provider's API natively supports. Locked in Aug
# 2026 per product decision.
TARGET_WIDTH = 1229
TARGET_HEIGHT = 1536


async def generate_images(prompt: str, count: int = 2, reference_image: bytes | None = None) -> list[bytes]:
    """
    Returns a list of raw image bytes (PNG), each exactly TARGET_WIDTH x
    TARGET_HEIGHT. Length may be less than `count` if some generations
    fail — callers should handle a possibly-short list, and treat an
    empty list as a full failure.

    reference_image: if given (e.g. a client's uploaded product photo),
    edits that image instead of generating a brand-new one from text alone
    -- see _edit_openai(). Best-effort: any failure there (bad response,
    provider quirk) falls back to plain text-to-image generation rather
    than failing the whole request, since a from-scratch creative is still
    far better than none.
    """
    if settings.IMAGE_PROVIDER == "openai":
        if reference_image is not None:
            edited = await _edit_openai(prompt, count, reference_image)
            if edited:
                return [_fit_to_target_size(img) for img in edited]
            logger.warning("Image edit produced nothing usable — falling back to text-to-image generation")
        results = await _generate_openai(prompt, count)
        return [_fit_to_target_size(img) for img in results]

    raise NotImplementedError(
        f"Image provider '{settings.IMAGE_PROVIDER}' not implemented yet. "
        "Add a branch here after benchmarking (see Week 2 Day 10-11 in the build guide)."
    )


def _fit_to_target_size(image_bytes: bytes) -> bytes:
    """
    Center-crops to the target aspect ratio (no stretching/distortion),
    then uniformly upscales to exactly TARGET_WIDTH x TARGET_HEIGHT.

    Why not just request the target size from the API directly: gpt-image-2
    (like gpt-image-1) only accepts a fixed enum of sizes -- "1024x1024",
    "1024x1536", "1536x1024", "auto" -- there's no way to request 1229x1536
    directly. _generate_openai/_edit_openai request "1024x1536" (native
    portrait, ratio 0.667) rather than square specifically so this crop
    only has to remove ~8% off the TOP and BOTTOM to reach the ~0.8 target
    ratio, leaving the full width untouched -- headline text lives on the
    horizontal axis, so a square source (which this used to request) needed
    a ~20%-of-width crop instead and was clipping headline text that
    extended into that discarded margin (see Aug 2026 incident: "Treat
    Yourselves" rendering as "reat Yourselves"). This function still
    handles a width-crop branch too, defensively, in case the source isn't
    what's expected.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        target_ratio = TARGET_WIDTH / TARGET_HEIGHT
        src_ratio = img.width / img.height

        if src_ratio > target_ratio:
            # Source is relatively wider than the target -- crop width.
            crop_w = round(img.height * target_ratio)
            left = (img.width - crop_w) // 2
            img = img.crop((left, 0, left + crop_w, img.height))
        elif src_ratio < target_ratio:
            # Source is relatively taller than the target -- crop height.
            crop_h = round(img.width / target_ratio)
            top = (img.height - crop_h) // 2
            img = img.crop((0, top, img.width, top + crop_h))

        img = img.resize((TARGET_WIDTH, TARGET_HEIGHT), Image.LANCZOS)

        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        logger.exception("Failed to fit generated image to target size — returning it unmodified")
        return image_bytes


async def _generate_openai(prompt: str, count: int) -> list[bytes]:
    """
    Calls OpenAI's image generation endpoint `count` times concurrently
    (one image per request is simplest and most reliable for retry logic).

    Model: gpt-image-2 (switched from gpt-image-1 — Aug 2026). Chosen
    specifically for its much stronger non-Latin script text rendering
    (independently verified for Hindi/Devanagari and Kannada; Tamil,
    Telugu, and Malayalam are less explicitly confirmed and should be
    spot-checked against real output before trusting them at scale — see
    app/i18n.py). VERIFY the response shape below (b64_json under
    data[0]) still matches gpt-image-2's actual API before the first real
    production run — this was switched without a live test against the
    new model.
    """
    url = "https://api.openai.com/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {settings.IMAGE_API_KEY}",
        "Content-Type": "application/json",
    }

    async def _one_call():
        payload = {
            "model": "gpt-image-2",
            "prompt": prompt,
            "size": "1024x1536",
            "n": 1,
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                logger.error("Image gen failed: %s | %s", resp.status_code, resp.text[:500])
                return None
            data = resp.json()
            b64 = data["data"][0]["b64_json"]
            return base64.b64decode(b64)

    results = await asyncio.gather(*[_one_call() for _ in range(count)])
    return [r for r in results if r is not None]


async def _edit_openai(prompt: str, count: int, reference_image: bytes) -> list[bytes]:
    """
    Calls OpenAI's image EDIT endpoint (multipart/form-data, not JSON) with
    the client's uploaded photo as the base — e.g. "change the background to
    black" actually modifies their real product photo instead of generating
    an unrelated new image from a text description.

    UNVERIFIED against a live call, same caveat as _generate_openai() above
    for gpt-image-2: the multipart field names/response shape here match
    OpenAI's documented /v1/images/edits contract as of this writing, but
    double-check against the live docs before the first real production
    run. Any failure here is caught by the caller (generate_images), which
    falls back to plain text-to-image generation rather than failing the
    whole request.
    """
    url = "https://api.openai.com/v1/images/edits"
    headers = {"Authorization": f"Bearer {settings.IMAGE_API_KEY}"}

    async def _one_call():
        files = {"image": ("reference.png", reference_image, "image/png")}
        data = {"model": "gpt-image-2", "prompt": prompt, "size": "1024x1536", "n": "1"}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, files=files, data=data)
            if resp.status_code >= 400:
                logger.error("Image edit failed: %s | %s", resp.status_code, resp.text[:500])
                return None
            body = resp.json()
            b64 = body["data"][0]["b64_json"]
            return base64.b64decode(b64)

    try:
        results = await asyncio.gather(*[_one_call() for _ in range(count)])
        return [r for r in results if r is not None]
    except Exception:
        logger.exception("Image edit request failed entirely")
        return []
