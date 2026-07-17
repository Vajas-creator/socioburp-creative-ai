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

from anthropic import AsyncAnthropic

from app.config import settings

logger = logging.getLogger("socioburp.engine.quality")

client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

REGEN_THRESHOLD = 60

SYSTEM_PROMPT = """You are a strict quality reviewer for social media marketing
creatives aimed at Indian small businesses. Score each image 0-100 on:
- Headline text rendered correctly, no gibberish or spelling errors (40 pts)
- Looks like a professional ad, not obvious/uncanny AI art (25 pts)
- Text is readable against the background, good contrast (20 pts)
- Composition: clear space for a logo, not cluttered (15 pts)

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

        response = await client.messages.create(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)

        best_index = parsed["best_index"]
        best_score = parsed["scores"][best_index]

        return {"best_index": best_index, "best_score": best_score, "issues": parsed.get("issues", [])}

    except Exception:
        logger.exception("Quality check failed — defaulting to first image, neutral score.")
        return {"best_index": 0, "best_score": 65, "issues": ["quality check unavailable"]}
