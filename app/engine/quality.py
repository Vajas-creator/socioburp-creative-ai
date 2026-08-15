"""
Quality gate. Scores each candidate image 0-100 via Claude vision and picks
the best. If the best score is still below the regen threshold, the caller
(orchestrator.py) regenerates once — never more than once, to bound cost —
and sends the result regardless of score after that, logging it for nightly
pilot review per the build guide.
"""
import base64
import json
import logging


from app.config import settings

logger = logging.getLogger("socioburp.engine.quality")

from app.anthropic_client import create_message
from app.json_extract import extract_json_text

REGEN_THRESHOLD = 60

SYSTEM_PROMPT = """You are a strict quality reviewer for social media marketing
creatives aimed at Indian small businesses. These images have already been
resized to their final delivery dimensions before you see them (extended
with new content into fresh margins where needed, not cropped, as of Aug
2026), so evaluate them exactly as they are -- any clipping you see is real
and already final, not an artifact of a resize step still to come.

There is deliberately NO text of any kind on these images yet -- headline
text gets added afterward by a separate, deterministic, code-based
compositing step, not by the image model. Do not penalize an image for
lacking a headline, and do not evaluate text quality/spelling/legibility at
all; that no longer applies at this stage.

FIRST, check for a disqualifying defect on each image: is the product/
subject itself, or any other visually important element, touching, cut off
by, or extending past ANY edge of the image -- especially the top or bottom
edge? A product missing part of itself at the frame edge is a hard fail. If
this defect is present, that image's score CANNOT exceed 40, no matter how
good everything else about it looks -- do not average this against the
other criteria below, cap it outright.

Otherwise, score 0-100 on:
- Product/subject rendered in sharp, crisp, well-lit, photorealistic
  detail -- not soft, blurry, generic-looking, or low-detail (35 pts)
- Looks like a professional ad, not obvious/uncanny AI art (25 pts)
- Composition: has genuine uncluttered space where a headline and a logo
  could both be added afterward without covering the subject, not
  cluttered edge-to-edge (25 pts)
- Color/style coherence with a cohesive brand look (15 pts)

Reply with JSON only, no other text:
{"scores": [n1, n2, ...], "best_index": 0, "issues": ["short issue 1", "short issue 2"]}

best_index is the 0-based index of the highest-scoring image.
issues is a short list of problems found in the best image, empty list if none."""


async def score_and_pick(images: list[bytes]) -> dict:
    """
    Returns {"best_index": int, "best_score": int, "issues": list[str]}
    On any failure, defaults to picking image 0 with a neutral score so the
    pipeline doesn't dead-end — quality issues will just be caught in
    nightly manual review during the pilot instead.
    """
    if not images:
        return {"best_index": -1, "best_score": 0, "issues": ["no images generated"]}

    if len(images) == 1:
        # Nothing to compare — skip the API call, save the cost
        return {"best_index": 0, "best_score": 70, "issues": []}

    try:
        content = [{"type": "text", "text": "Score these candidate creatives:"}]
        for img_bytes in images:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(img_bytes).decode("utf-8"),
                },
            })

        response = await create_message(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        text = response.content[0].text.strip()
        text = extract_json_text(text)
        parsed = json.loads(text)

        best_index = parsed["best_index"]
        best_score = parsed["scores"][best_index]

        return {"best_index": best_index, "best_score": best_score, "issues": parsed.get("issues", [])}

    except Exception:
        logger.exception("Quality check failed — defaulting to first image, neutral score.")
        return {"best_index": 0, "best_score": 65, "issues": ["quality check unavailable"]}
