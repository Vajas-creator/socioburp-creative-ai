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
import re

import httpx
from PIL import Image

from app.config import settings

logger = logging.getLogger("socioburp.engine.image_gen")

# Every generated creative must ship at exactly this size (~4:5 portrait),
# regardless of what the provider's API natively supports. Locked in Aug
# 2026 per product decision.
TARGET_WIDTH = 1229
TARGET_HEIGHT = 1536

# Aug 2026 "OpenAI rate limit killing whole carousels" fix -- see
# _post_with_rate_limit_retry()'s docstring below.
_RATE_LIMIT_RETRIES = 3          # total attempts, including the first
_RATE_LIMIT_MAX_WAIT = 30.0      # never wait longer than this, even if the provider suggests more
_RATE_LIMIT_DEFAULT_WAIT = 5.0   # used if the 429 body doesn't include a parseable suggested wait
_RETRY_AFTER_RE = re.compile(r"try again in (\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


def _parse_retry_after_seconds(resp: httpx.Response) -> float | None:
    """Pulls the provider's own suggested wait time out of a 429 body, e.g. '...try again in 12s.'"""
    try:
        message = resp.json().get("error", {}).get("message", "")
    except Exception:
        return None
    match = _RETRY_AFTER_RE.search(message)
    if not match:
        return None
    return min(float(match.group(1)) + 0.5, _RATE_LIMIT_MAX_WAIT)  # small buffer, capped


async def _post_with_rate_limit_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """
    POSTs to `url`, retrying automatically on a 429 (rate limit) response
    using the provider's own suggested wait time when given, or a fixed
    default otherwise.

    Real production incident this fixes: a carousel fans out N slides x 2
    candidates concurrently (see generate_images()'s callers), which can
    easily burst past OpenAI's gpt-image-2 requests-per-minute cap on the
    account. Previously, whichever call got rejected with a 429 failed
    immediately and permanently -- generate_images() returned an empty
    list for that candidate, and if BOTH of a slide's candidates hit
    this, orchestrator.py raised "No image returned for carousel slide
    N", which killed the ENTIRE carousel (asyncio.gather propagates the
    first exception), throwing away every OTHER slide that had already
    generated successfully. OpenAI's own 429 body tells us exactly how
    long the limit takes to reset ("Please try again in 12s") -- a short
    wait-and-retry turns most of these into quiet successes instead.

    Does NOT retry on any other status code -- a real 4xx/5xx error (bad
    request, auth failure, server error) won't be fixed by waiting, so
    retrying it would just waste time before the caller's existing
    failure handling kicks in anyway.
    """
    resp = None
    for attempt in range(_RATE_LIMIT_RETRIES):
        resp = await client.post(url, **kwargs)
        if resp.status_code != 429:
            return resp
        if attempt == _RATE_LIMIT_RETRIES - 1:
            break
        wait_s = _parse_retry_after_seconds(resp) or _RATE_LIMIT_DEFAULT_WAIT
        logger.warning(
            "OpenAI rate limit hit (attempt %d/%d) — waiting %.1fs before retry: %s",
            attempt + 1, _RATE_LIMIT_RETRIES, wait_s, resp.text[:300],
        )
        await asyncio.sleep(wait_s)
    return resp


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
                return await asyncio.gather(*[_extend_to_target_size(img) for img in edited])
            logger.warning("Image edit produced nothing usable — falling back to text-to-image generation")
        results = await _generate_openai(prompt, count)
        return await asyncio.gather(*[_extend_to_target_size(img) for img in results])

    raise NotImplementedError(
        f"Image provider '{settings.IMAGE_PROVIDER}' not implemented yet. "
        "Add a branch here after benchmarking (see Week 2 Day 10-11 in the build guide)."
    )


async def _extend_to_target_size(image_bytes: bytes) -> bytes:
    """
    Aug 2026 "my image and text still cut" deep-dive: the OLD approach
    here center-CROPPED the source down to the target ratio, which meant
    anything the model drew too close to the edge (despite prompt
    instructions to leave margin) got destroyed -- no amount of prompt
    tightening can make a probabilistic model respect a hard pixel
    boundary 100% of the time, so a fixed margin was NEVER going to fully
    eliminate this.

    This instead EXTENDS the canvas via AI outpainting -- the source
    already matches the target HEIGHT exactly (1536), so only WIDTH needs
    growing (1024 -> 1229, ~102px added on each side). Nothing that was
    actually drawn is ever removed; the model only ever ADDS new content
    into the fresh margins, continuing the existing scene. This makes
    "content cut off by the resize step" categorically impossible, not
    just less likely.

    Falls back to the old crop-based fit (_crop_to_target_size) if the
    outpaint call itself fails for any reason -- strictly no worse than
    the previous behavior, never silently returns something broken.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        if img.width == TARGET_WIDTH and img.height == TARGET_HEIGHT:
            out = io.BytesIO()
            img.save(out, format="PNG")
            return out.getvalue()

        if img.height != TARGET_HEIGHT or img.width > TARGET_WIDTH:
            # Not the expected native 1024x1536 shape (defensive -- e.g. a
            # provider/size change) -- outpainting math below assumes a
            # pure width extension at matching height, so fall back to the
            # old crop-based fit rather than risk a malformed canvas/mask.
            logger.warning(
                "Source image is %sx%s, not the expected 1024x%s — falling back to crop-based fit",
                img.width, img.height, TARGET_HEIGHT,
            )
            return _crop_to_target_size(image_bytes)

        outpainted = await _outpaint_openai(img)
        if outpainted is not None:
            return outpainted

        logger.warning("Outpaint extend failed — falling back to crop-based fit")
        return _crop_to_target_size(image_bytes)

    except Exception:
        logger.exception("Failed to extend generated image to target size — falling back to crop-based fit")
        return _crop_to_target_size(image_bytes)


async def _outpaint_openai(img: Image.Image) -> bytes | None:
    """
    Extends `img` (assumed TARGET_HEIGHT tall, narrower than TARGET_WIDTH)
    to exactly TARGET_WIDTH x TARGET_HEIGHT by outpainting new content
    into the added left/right margins via OpenAI's image-edit endpoint.
    Returns None on any failure -- caller falls back to cropping.
    """
    canvas = Image.new("RGB", (TARGET_WIDTH, TARGET_HEIGHT), (0, 0, 0))
    paste_x = (TARGET_WIDTH - img.width) // 2
    canvas.paste(img, (paste_x, 0))

    # Mask per OpenAI's edit contract: transparent (alpha=0) = "generate
    # here", opaque (alpha=255) = "preserve exactly as-is".
    mask = Image.new("RGBA", (TARGET_WIDTH, TARGET_HEIGHT), (0, 0, 0, 0))
    preserved = Image.new("RGBA", (img.width, img.height), (0, 0, 0, 255))
    mask.paste(preserved, (paste_x, 0))

    canvas_buf = io.BytesIO()
    canvas.save(canvas_buf, format="PNG")
    mask_buf = io.BytesIO()
    mask.save(mask_buf, format="PNG")

    url = "https://api.openai.com/v1/images/edits"
    headers = {"Authorization": f"Bearer {settings.IMAGE_API_KEY}"}
    files = {
        "image": ("canvas.png", canvas_buf.getvalue(), "image/png"),
        "mask": ("mask.png", mask_buf.getvalue(), "image/png"),
    }
    data = {
        "model": "gpt-image-2",
        "prompt": (
            "Extend this image naturally to fill the surrounding transparent margins, "
            "continuing the existing background/scene seamlessly. Do not add any new text, "
            "logos, or subjects -- just a natural continuation of what's already there."
        ),
        "size": "auto",
        "n": "1",
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await _post_with_rate_limit_retry(client, url, headers=headers, files=files, data=data)
        if resp.status_code >= 400:
            logger.error("Outpaint extend failed: %s | %s", resp.status_code, resp.text[:500])
            return None
        body = resp.json()
        b64 = body["data"][0]["b64_json"]
        result_bytes = base64.b64decode(b64)

        # The edit endpoint may not return EXACTLY TARGET_WIDTHxTARGET_HEIGHT
        # depending on what size it actually honors -- normalize with a
        # simple resize (no crop) as a final guarantee of exact dimensions.
        result_img = Image.open(io.BytesIO(result_bytes)).convert("RGB")
        if result_img.size != (TARGET_WIDTH, TARGET_HEIGHT):
            result_img = result_img.resize((TARGET_WIDTH, TARGET_HEIGHT), Image.LANCZOS)
        out = io.BytesIO()
        result_img.save(out, format="PNG")
        return out.getvalue()

    except Exception:
        logger.exception("Outpaint extend request failed entirely")
        return None


def _crop_to_target_size(image_bytes: bytes) -> bytes:
    """
    Center-crops to the target aspect ratio (no stretching/distortion),
    then uniformly upscales to exactly TARGET_WIDTH x TARGET_HEIGHT.

    Kept as the fallback path for _extend_to_target_size() above (used
    only if the outpaint API call itself fails) -- this was the PRIMARY
    resize strategy before Aug 2026's "my image and text still cut"
    deep-dive, and is strictly worse (can destroy content near the
    edges), but "sometimes still crops if outpainting is down" beats
    "the whole generation fails".
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        target_ratio = TARGET_WIDTH / TARGET_HEIGHT
        src_ratio = img.width / img.height

        if src_ratio > target_ratio:
            crop_w = round(img.height * target_ratio)
            left = (img.width - crop_w) // 2
            img = img.crop((left, 0, left + crop_w, img.height))
        elif src_ratio < target_ratio:
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


def _fit_to_target_size(image_bytes: bytes) -> bytes:
    """
    Backward-compatible sync alias for _crop_to_target_size() -- still
    used directly by app/engine/image_intent.py's "use as-is" path (a
    client's OWN uploaded photo, not a generation, so there's no source
    to outpaint from; simple crop-to-fit is correct there).
    """
    return _crop_to_target_size(image_bytes)


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
            resp = await _post_with_rate_limit_retry(client, url, headers=headers, json=payload)
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
            resp = await _post_with_rate_limit_retry(client, url, headers=headers, files=files, data=data)
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
