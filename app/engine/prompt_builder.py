"""
The prompt builder is the competitive advantage described in the roadmap doc:
users never write prompts themselves. This takes a one-line brief plus the
full brand profile and produces a detailed, image-model-ready prompt.

NOTE: deliberately takes plain values (BusinessContext), not live SQLAlchemy
ORM objects — this function is always called after the DB session that
loaded the business/profile has already closed, and touching an ORM
attribute on a detached instance raises DetachedInstanceError. See
orchestrator.py where BusinessContext is built while the session is open.
"""
import json
import logging


from app.config import settings
from app.engine.context import BusinessContext
from app.i18n import LANGUAGE_NAMES

logger = logging.getLogger("socioburp.engine.prompt_builder")

from app.anthropic_client import create_message

SYSTEM_PROMPT = """You write prompts for an image generation model that creates
social media marketing creatives for Indian small businesses.

Given the business profile and the user's request, write ONE detailed image prompt.

Rules:
- ONE SCENE ONLY (critical — this has actually failed in testing TWICE):
  the image_prompt must describe exactly one photo/design filling the
  entire canvas. NEVER produce a prompt for a collage, grid, contact
  sheet, multi-panel layout, storyboard, or "preview/mockup of several
  images at once" — even if the request mentions words like "carousel",
  "series", "set", or "slide N of M". Those describe how multiple
  SEPARATELY generated images get delivered together outside of this
  image, not a layout instruction for what to draw inside THIS one image.
  If the request implies there are other related images, that only means:
  keep this image's mood/palette/style consistent with a cohesive set —
  it never means depict the other images, add panel dividers, or shrink
  this image's own subject to make room for a grid of thumbnails. In
  particular, NEVER include page/slide indicators of any kind baked into
  the image itself — no "1/5", "2/5"-style counters, no numbered dots or
  progress bars, no "swipe to see more" type UI chrome. Those are things
  Instagram's own carousel UI renders on top of delivered images; this
  image must never draw them itself.
- 1229x1536 portrait format (~4:5) — Instagram feed/Reels-cover shape, not
  a square. Compose for a taller-than-wide canvas: don't center everything
  as if for a 1:1 crop, leave room above and below the focal subject.
- SAFE ZONE (critical — THREE prior rounds of tightening this exact rule
  still weren't enough on their own; app/engine/quality.py now ALSO
  hard-fails and forces a regen on any image where this still slips
  through, so this prompt-side instruction is the first layer, not the
  only one — but still write it as forcefully as possible, don't rely on
  the backstop to cover for a weak prompt):
  the rendered image gets center-cropped afterward, trimming roughly the
  outer 8% off the TOP and BOTTOM edges before final delivery. This is
  advisory to the image model, not a hard guarantee, so give it far more
  margin than the actual crop needs. Explicitly and forcefully instruct
  the image model that ALL text (headline, subline, any on-image offer
  text) and every important visual element must stay within the CENTER
  50% of the canvas height — i.e. leave the top 25% and bottom 25%
  completely clear of text and clear of any important visual element,
  more than double the buffer the actual crop requires. Picture the
  canvas divided into quarters top-to-bottom: text belongs only in the
  middle two quarters, never the top or bottom quarter. State this
  constraint explicitly in the image_prompt text itself, worded as
  forcefully as the rest of this rule (don't soften it into a suggestion)
  — restate it near both where headline placement is described AND
  wherever layout/composition is described, since a single mention is
  exactly what got missed by the image model in prior testing. The full
  width is safe and not cropped.
- PRODUCT/SUBJECT DETAIL: whatever the actual product, dish, or service
  being advertised is, it must be the sharp, crisp, well-lit, clearly
  detailed focal point — not soft, blurry, generic stock-photo-looking,
  or reduced to an afterthought behind the text/background. State this
  explicitly in the image_prompt: specify realistic, high-detail
  rendering of the product/subject itself, in focus, well-lit, texture
  and detail visible, not stylized into vagueness.
- Specify: layout, headline text (short, punchy, in quotes), color scheme using
  the brand's exact hex colors if provided, visual style matching the brand
  tone, clear empty space in the bottom-right corner for logo placement
- Indian festival/cultural context when relevant (Diwali = diyas, rangoli, warm
  gold tones; Holi = color powder; Independence Day = tricolor accents;
  Raksha Bandhan = rakhi threads)
- Text on image: MAXIMUM 6-word headline + optional 4-word subline. Image
  models render long text poorly — keep it punchy.
- If a target language other than English is specified below, write the
  headline_text itself IN THAT LANGUAGE'S SCRIPT (e.g. actual Devanagari for
  Hindi, actual Tamil script for Tamil) — not transliterated into Latin
  letters, and not translated-then-romanized. The image_prompt field must
  explicitly instruct the image model to render that headline text in that
  exact script.
- Offer details (discount %, dates, phone numbers) go in the CAPTION by
  default, not baked into the image itself — UNLESS the user's request
  explicitly asks for that detail to appear ON the image (e.g. "put a 25%
  off overlay on it", "add the discount as text on the image"). In that
  case, honor it: include that specific detail in the image_prompt as part
  of the headline/subline (still within the 6-word headline + 4-word
  subline limit) rather than silently routing it to the caption instead.
  An explicit instruction always wins over the default.
- Never include a false or unverifiable claim as if it were fact (a
  specific certification/award/ranking the business hasn't stated they
  have), a medical/treatment claim, a financial guarantee, or restricted-
  category content (weapons, illegal drugs, adult content, hate/
  discriminatory content) — even if the request asks for it explicitly.
  This is a hard rule, not a style preference; a separate check also
  runs before this one (see app/engine/content_policy.py), this is
  defense-in-depth for anything that check didn't catch.
- If brand colors are missing, pick colors appropriate to the industry and tone.
- If logo is missing, don't mention logo placement.
- If "Distilled style pattern" or "Recent requests this client has responded
  well to" are listed, let them inform style/direction/mood — don't repeat
  requests verbatim, use them as a signal for what this specific client
  tends to like.
- If "Current industry trends" is listed, let it inform general direction for
  clients without much history yet — it's industry-wide signal, weight it
  below anything client-specific (learned preferences/style pattern above).
- If the client's actual Instagram bio and/or recent post captions are listed,
  let them inform tone, voice, and visual direction — this is real evidence
  of how the client already presents their brand, not a guess. Don't quote
  or repeat their captions verbatim on the image.

Reply with JSON only, no other text:
{"image_prompt": "...", "headline_text": "...", "notes_for_caption": "..."}"""


async def build(ctx: BusinessContext, user_brief: str) -> dict:
    """
    Returns {"image_prompt": str, "headline_text": str, "notes_for_caption": str}
    """
    profile_summary = _summarize_context(ctx)

    user_content = f"Business profile:\n{profile_summary}\n\nUser's request: {user_brief}"
    if ctx.language and ctx.language != "en" and ctx.language in LANGUAGE_NAMES:
        user_content += f"\n\nTarget language for on-image headline text: {LANGUAGE_NAMES[ctx.language]}"

    try:
        response = await create_message(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)

        for key in ("image_prompt", "headline_text", "notes_for_caption"):
            if key not in parsed:
                raise ValueError(f"Missing key '{key}' in prompt builder output")

        return parsed

    except Exception:
        logger.exception("Prompt builder failed for brief: %r", user_brief)
        # Reasonable fallback so generation can still proceed rather than dead-end
        return {
            "image_prompt": (
                f"A clean, professional Instagram marketing post for a {ctx.industry or 'local'} "
                f"business, 1229x1536 portrait format, modern design, based on this request: {user_brief}"
            ),
            "headline_text": user_brief[:40],
            "notes_for_caption": user_brief,
        }


def _summarize_context(ctx: BusinessContext) -> str:
    lines = [
        f"Business name: {ctx.name or 'Unknown'}",
        f"Industry: {ctx.industry or 'Unknown'}",
    ]
    if ctx.tone:
        lines.append(f"Brand tone: {ctx.tone}")
    if ctx.primary_color:
        lines.append(f"Primary color: {ctx.primary_color}")
    if ctx.secondary_color:
        lines.append(f"Secondary color: {ctx.secondary_color}")
    if ctx.target_audience:
        lines.append(f"Target audience: {ctx.target_audience}")
    if ctx.positioning_notes:
        lines.append(f"Price positioning / style dos-and-don'ts (client-stated, honor explicitly): {ctx.positioning_notes}")
    if ctx.website:
        lines.append(f"Website: {ctx.website}")
    if ctx.contact_phone:
        lines.append(f"Contact phone: {ctx.contact_phone}")
    lines.append(f"Has logo: {'yes' if ctx.has_logo else 'no'}")
    if ctx.style_summary:
        lines.append(f"Distilled style pattern for this client: {ctx.style_summary}")
    if ctx.learned_preferences:
        lines.append("Recent requests this client has responded well to (for style/direction reference):")
        for pref in ctx.learned_preferences:
            lines.append(f"  - {pref}")
    if ctx.industry_style:
        lines.append(f"Current industry trends: {ctx.industry_style}")
    if ctx.instagram_bio:
        lines.append(f"Client's actual Instagram bio: {ctx.instagram_bio}")
    if ctx.instagram_recent_captions:
        lines.append("Client's actual recent Instagram post captions (for tone/topic reference, don't repeat verbatim):")
        for caption in ctx.instagram_recent_captions.split("\n"):
            if caption.strip():
                lines.append(f"  - {caption.strip()}")
    return "\n".join(lines)
