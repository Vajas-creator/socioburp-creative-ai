"""
Revision classifier — runs at the top of the REVISE branch, before any paid
pipeline work. Decides whether the requested change is:

  LOGO_POSITION     - the client only wants the logo moved ("logo top left",
                      "put the logo in the corner", "move logo to center").
                      This never needs a new image: we still have the parent
                      generation's pre-composite background (base_image_url),
                      so we just re-paste the logo at the new spot. Free —
                      no Claude prompt build, no image generation, no quality
                      check, no credit charged.

  FULL_REGENERATION - anything else ("make it more premium", "change the
                      headline", "brighter colors") — a real creative change
                      that needs the normal revision pipeline and charges
                      normally.

Cheap Haiku call, same pattern as intent.py. Falls back to FULL_REGENERATION
on any failure — the worst case is the client pays the normal price for a
logo move, never that a real creative change gets skipped.
"""
import json
import logging


from app.config import settings

logger = logging.getLogger("socioburp.engine.revision_classifier")

from app.anthropic_client import create_message

VALID_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "center")

SYSTEM_PROMPT = """A client is asking for a change to an already-generated marketing creative.
Classify the request into exactly one revision type:

- LOGO_POSITION: the ONLY change requested is moving/repositioning the logo
  ("move the logo to the top left", "logo in the other corner", "put logo in
  the middle", "logo thoda upar karo"). If they ask for ANYTHING else too
  (colors, text, style, size of the logo), it is NOT a LOGO_POSITION request.

- FULL_REGENERATION: any other change — style, colors, text, layout, mood,
  or a mix of logo placement plus something else.

For LOGO_POSITION, also give the target position as exactly one of:
top-left | top-right | bottom-left | bottom-right | center
("the other corner" or similar without a clear side -> pick the most likely
one from context; "middle"/"center" -> center).

Reply with JSON only, no other text:
{"revision_type": "LOGO_POSITION", "position": "top-left|top-right|bottom-left|bottom-right|center", "brief": "one-line summary in English"}
or
{"revision_type": "FULL_REGENERATION", "brief": "one-line summary of the requested change, in English"}"""


async def classify(user_message: str) -> dict:
    """
    Returns either:
      {"revision_type": "LOGO_POSITION", "position": str, "brief": str}
      {"revision_type": "FULL_REGENERATION", "brief": str}
    Falls back to FULL_REGENERATION on any failure — the normal pipeline can
    handle a logo move too (just not for free), so this is the safe direction.
    """
    try:
        response = await create_message(
            model=settings.CLAUDE_INTENT_MODEL,
            max_tokens=150,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text.strip()
        # Strip markdown fences if Claude adds them despite instructions
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)

        if parsed.get("revision_type") not in ("LOGO_POSITION", "FULL_REGENERATION"):
            raise ValueError(f"Unexpected revision_type: {parsed.get('revision_type')}")

        if parsed["revision_type"] == "LOGO_POSITION":
            if parsed.get("position") not in VALID_POSITIONS:
                raise ValueError(f"Unexpected position: {parsed.get('position')}")

        parsed.setdefault("brief", user_message)
        return parsed

    except Exception:
        logger.exception("Revision classification failed for message: %r", user_message)
        return {"revision_type": "FULL_REGENERATION", "brief": user_message}
