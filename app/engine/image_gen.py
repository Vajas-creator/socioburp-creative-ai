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
import logging

import httpx

from app.config import settings

logger = logging.getLogger("socioburp.engine.image_gen")


async def generate_images(prompt: str, count: int = 2) -> list[bytes]:
    """
    Returns a list of raw image bytes (PNG). Length may be less than `count`
    if some generations fail — callers should handle a possibly-short list,
    and treat an empty list as a full failure.
    """
    if settings.IMAGE_PROVIDER == "openai":
        return await _generate_openai(prompt, count)

    raise NotImplementedError(
        f"Image provider '{settings.IMAGE_PROVIDER}' not implemented yet. "
        "Add a branch here after benchmarking (see Week 2 Day 10-11 in the build guide)."
    )


async def _generate_openai(prompt: str, count: int) -> list[bytes]:
    """
    Calls OpenAI's image generation endpoint `count` times concurrently
    (one image per request is simplest and most reliable for retry logic).
    """
    url = "https://api.openai.com/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {settings.IMAGE_API_KEY}",
        "Content-Type": "application/json",
    }

    async def _one_call():
        payload = {
            "model": "gpt-image-1",
            "prompt": prompt,
            "size": "1024x1024",
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
