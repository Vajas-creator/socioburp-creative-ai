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
from app.json_extract import extract_json_text

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
- NO TEXT OF ANY KIND ON THE IMAGE (critical — this changed Aug 2026, and
  overrides anything that sounds like it's asking for on-image text
  elsewhere in this profile or the user's request): never render any
  words, letters, numbers, or typography anywhere in the image itself —
  no headline, no subline, no offer text, no price, no phone number, no
  page/slide counters, nothing. The actual headline gets added afterward
  by a separate, deterministic, code-based text-rendering step (real
  fonts, not a diffusion model guessing at glyphs) that composites
  cleanly on top of this image once it's finished — so this image must be
  a clean photo/design with NO text baked in at all, full stop. State
  this explicitly and forcefully in the image_prompt itself.
- COMPOSITION: this canvas gets extended (not cropped) to its final
  1229x1536 shape afterward — new content is painted into fresh margins
  on the sides, nothing that's actually drawn here gets removed. Normal
  good composition practice still applies: give the main subject some
  breathing room rather than pressing it flush against the raw edges of
  the frame, but there's no special crop zone to defend against here
  anymore.
- PRODUCT/SUBJECT DETAIL: whatever the actual product, dish, or service
  being advertised is, it must be the sharp, crisp, well-lit, clearly
  detailed focal point — not soft, blurry, generic stock-photo-looking,
  or reduced to an afterthought behind the text/background. State this
  explicitly in the image_prompt: specify realistic, high-detail
  rendering of the product/subject itself, in focus, well-lit, texture
  and detail visible, not stylized into vagueness.
- Specify in image_prompt: layout, color scheme using the brand's exact hex
  colors if provided, visual style matching the brand tone, and clear,
  uncluttered empty space reserved for a logo — by default in the
  bottom-right corner, UNLESS the business profile below states a logo
  placement preference, in which case reserve the space there instead
  (the actual logo is composited on afterward by a separate vision step
  that picks the exact spot within whatever clear space exists — your job
  here is only to make sure clear space actually exists in the right
  general area, not to place the logo yourself). Do NOT mention headline
  text, a subline, or any words at all in image_prompt — see the NO TEXT
  rule above.
- headline_text / subtext_text / cta_text (three separate JSON fields, NOT
  part of image_prompt — this is the ENTIRE set of on-image text this
  request can ever have, since the image itself has none):
    - headline_text (required): the bold, dominant line — short and
      punchy, think "how a real person would text it", not ad copy —
      MAXIMUM 6 words.
    - subtext_text (optional, "" if not needed): one small supporting
      line beneath the headline — a short explanatory phrase, MAXIMUM
      ~12 words. Only include this if the request actually calls for
      supporting text (e.g. "with a small subtext line", "add a tagline
      under it") or the business profile's tone clearly benefits from
      one — don't invent one for a simple request that only needs a
      headline.
    - cta_text (optional, "" if not needed): one even smaller line for a
      call-to-action, website, contact detail, or offer specifics
      explicitly requested to appear ON the image (e.g. "add the website
      at the bottom", "put a 25% off overlay on it", "add DM us to get
      started"). MAXIMUM ~8 words. Leave "" unless the request or profile
      explicitly calls for this.
  All three get rendered afterward as real, crisp text by a separate
  deterministic compositing step (app/engine/text_overlay.py), stacked
  headline-then-subtext-then-cta, not painted by the image model, so
  there's no risk of any of them coming out garbled or cut off — keep
  each one short because that's better marketing copy and better visual
  hierarchy, not because of any rendering limit.
- If a target language other than English is specified below, write
  headline_text/subtext_text/cta_text themselves IN THAT LANGUAGE'S
  SCRIPT (e.g. actual Devanagari for Hindi, actual Tamil script for
  Tamil) — not transliterated into Latin letters, and not
  translated-then-romanized.
- Offer details (discount %, dates, phone numbers) go in the CAPTION by
  default, not on the image — UNLESS the user's request explicitly asks
  for that detail to appear ON the image (e.g. "put a 25% off overlay on
  it", "add the discount as text on the image"). In that case, honor it:
  fold that specific detail into cta_text (or headline_text if it's the
  dominant point of the creative) rather than silently routing it to the
  caption. An explicit instruction always wins over the default.
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
{"image_prompt": "...", "headline_text": "...", "subtext_text": "", "cta_text": "", "notes_for_caption": "..."}"""


async def build(ctx: BusinessContext, user_brief: str) -> dict:
    """
    Returns {"image_prompt": str, "headline_text": str, "subtext_text": str,
    "cta_text": str, "notes_for_caption": str}. subtext_text/cta_text are
    "" when the request doesn't call for them.
    """
    profile_summary = _summarize_context(ctx)

    user_content = f"Business profile:\n{profile_summary}\n\nUser's request: {user_brief}"
    if ctx.language and ctx.language != "en" and ctx.language in LANGUAGE_NAMES:
        user_content += f"\n\nTarget language for on-image text: {LANGUAGE_NAMES[ctx.language]}"

    try:
        response = await create_message(
            model=settings.CLAUDE_PROMPT_MODEL,
            # Was 600 -- too tight once the SAFE ZONE rule started requiring
            # the constraint restated twice within image_prompt itself, plus
            # the product-detail instruction added alongside it (see Aug
            # 2026 "quality gate + product sharpness" round). Real
            # production symptom: json.loads() failing with "Unterminated
            # string" across many unrelated briefs -- the response was
            # being cut off mid-string before the JSON ever closed, not a
            # one-off content quirk. Bumped again to 1500 when subtext_text/
            # cta_text were added alongside headline_text (small on their
            # own, but real revision briefs can be very long -- see the
            # "Revise this existing creative concept" incident where a
            # 3-slide carousel's full prior description got concatenated
            # into one user_brief).
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        text = extract_json_text(text)
        parsed = json.loads(text)

        for key in ("image_prompt", "headline_text", "notes_for_caption"):
            if key not in parsed:
                raise ValueError(f"Missing key '{key}' in prompt builder output")
        parsed.setdefault("subtext_text", "")
        parsed.setdefault("cta_text", "")

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
            "subtext_text": "",
            "cta_text": "",
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
    if ctx.has_logo and ctx.logo_position_hint:
        lines.append(f"Logo placement preference (reserve empty space there instead of the default corner): {ctx.logo_position_hint}")
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
