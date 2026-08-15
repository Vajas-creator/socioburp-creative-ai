"""
Test for the Aug 2026 "grid colors should be consistent" fix.

Real gap surfaced by feedback: a business with no uploaded logo and no
explicitly stated brand colors had prompt_builder.build() ask Claude to
"pick colors appropriate to the industry and tone" fresh, independently,
on EVERY single generation call -- including independently across the
concurrently-built slides of ONE carousel (generate_carousel()'s
_build_one_slide() fans out via asyncio.gather with zero cross-slide
awareness of what colors a sibling slide picked). Nothing ever persisted
whatever got chosen, so consecutive posts (or even slides within a single
carousel) could each land on a visibly different color scheme -- an
Instagram grid that looks broken instead of cohesive.

Fix: app/engine/prompt_builder.py's resolve_colors() picks a palette ONCE
(informed by cached industry-trend research, i.e. genuinely "what other
businesses in this industry are doing", not an arbitrary guess) when a
business has no stored colors yet, and orchestrator.py's
_resolve_and_lock_colors() PERSISTS that choice to BrandProfile and
returns an updated BusinessContext for the caller to use everywhere in
that request -- so it's the last time this business's colors are ever
improvised, exactly like a logo's colors would be authoritative from then
on (see app/engine/logo_capture.py, the companion "why are my images the
wrong color" fix).

Covers:
  - resolve_colors(): a business with stored colors gets them back
    unchanged, no API call needed; a business with none gets a freshly
    resolved palette; a failed resolution call falls back to a safe
    neutral default rather than blocking generation.
  - orchestrator._resolve_and_lock_colors(): a colorless business's
    resolved palette gets PERSISTED to BrandProfile; a business that
    already has colors is a complete no-op (no API call, no DB write);
    the returned BusinessContext carries the resolved colors for the
    caller to actually use.
  - Within ONE carousel, every slide shares the SAME resolved colors
    (not independently improvised per slide) -- the actual "ugly
    inconsistent grid" bug this closes.
  - A second, later generation for the same (now-colored) business
    reuses the persisted colors instead of resolving again.
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_color_consistency.db"
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
os.environ.setdefault("R2_ACCOUNT_ID", "fake")
os.environ.setdefault("R2_ACCESS_KEY", "fake")
os.environ.setdefault("R2_SECRET_KEY", "fake")
os.environ.setdefault("R2_BUCKET", "fake")
os.environ.setdefault("R2_PUBLIC_BASE_URL", "https://fake.example.com")

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


from app import db as db_module  # noqa: E402
import app.models  # noqa: E402
db_module.Base.metadata.create_all(bind=db_module.engine)

from PIL import Image  # noqa: E402
from app.engine import prompt_builder, orchestrator as orch  # noqa: E402
from app.engine.context import BusinessContext  # noqa: E402


class _FakeContent:
    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeContent(text)]


async def test_resolve_colors_unit():
    print("=" * 60)
    print("TEST 1: resolve_colors() returns stored colors unchanged, no API call")
    print("=" * 60)
    calls = {"n": 0}

    async def fake_create_message_should_not_run(**kwargs):
        calls["n"] += 1
        return _FakeResponse('{"primary_color": "#000000"}')

    prompt_builder.create_message = fake_create_message_should_not_run
    ctx = BusinessContext(name="Test", industry="bakery", primary_color="#AABBCC", secondary_color="#DDEEFF")
    primary, secondary = await prompt_builder.resolve_colors(ctx)
    assert (primary, secondary) == ("#AABBCC", "#DDEEFF")
    assert calls["n"] == 0, "FAIL: resolve_colors() should never call the model when colors are already stored"
    print("PASS: stored colors returned as-is, no API call made\n")

    print("=" * 60)
    print("TEST 2: resolve_colors() picks a fresh palette when none is stored")
    print("=" * 60)

    async def fake_create_message_picks(**kwargs):
        return _FakeResponse('{"primary_color": "#E8B4B8", "secondary_color": "#FFF8F0"}')

    prompt_builder.create_message = fake_create_message_picks
    ctx2 = BusinessContext(name="Test Bakery", industry="bakery", tone="playful")
    primary, secondary = await prompt_builder.resolve_colors(ctx2)
    assert (primary, secondary) == ("#E8B4B8", "#FFF8F0")
    print(f"PASS: resolved {(primary, secondary)}\n")

    print("=" * 60)
    print("TEST 3: resolve_colors() falls back to a safe default if the call fails")
    print("=" * 60)

    async def fake_create_message_fails(**kwargs):
        raise RuntimeError("simulated failure")

    prompt_builder.create_message = fake_create_message_fails
    ctx3 = BusinessContext(name="Test", industry="bakery")
    primary, secondary = await prompt_builder.resolve_colors(ctx3)
    assert primary == prompt_builder._FALLBACK_PRIMARY_COLOR
    assert secondary == prompt_builder._FALLBACK_SECONDARY_COLOR
    print(f"PASS: fell back to {(primary, secondary)}\n")


async def test_resolve_and_lock_colors_persists():
    from app.db import get_session
    from app.models import Business, BrandProfile

    print("=" * 60)
    print("TEST 4: _resolve_and_lock_colors() persists a fresh palette to BrandProfile")
    print("=" * 60)

    async def fake_create_message_picks(**kwargs):
        return _FakeResponse('{"primary_color": "#87CEEB", "secondary_color": "#FFFFFF"}')

    prompt_builder.create_message = fake_create_message_picks

    with get_session() as db:
        biz = Business(phone="919999999970", name="Colorless Biz", industry="tech", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id

    ctx = BusinessContext(name="Colorless Biz", industry="tech")
    resolved_ctx = await orch._resolve_and_lock_colors(biz_id, ctx)

    assert resolved_ctx.primary_color == "#87CEEB" and resolved_ctx.secondary_color == "#FFFFFF", (
        f"FAIL: expected the returned ctx to carry the resolved colors, got {resolved_ctx.primary_color!r}/{resolved_ctx.secondary_color!r}"
    )
    with get_session() as db:
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == biz_id).first()
        assert profile is not None and profile.primary_color == "#87CEEB", (
            f"FAIL: expected the resolved color persisted to BrandProfile, got {profile.primary_color if profile else None!r}"
        )
    print("PASS: resolved colors persisted to BrandProfile and returned on the ctx\n")

    print("=" * 60)
    print("TEST 5: a SECOND call for the SAME (now-colored) business reuses the persisted colors, no new API call")
    print("=" * 60)
    calls = {"n": 0}

    async def fake_create_message_should_not_run(**kwargs):
        calls["n"] += 1
        return _FakeResponse('{"primary_color": "#000000"}')

    prompt_builder.create_message = fake_create_message_should_not_run

    with get_session() as db:
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == biz_id).first()
        ctx_with_stored = BusinessContext(
            name="Colorless Biz", industry="tech",
            primary_color=profile.primary_color, secondary_color=profile.secondary_color,
        )
    resolved_ctx2 = await orch._resolve_and_lock_colors(biz_id, ctx_with_stored)
    assert resolved_ctx2.primary_color == "#87CEEB"
    assert calls["n"] == 0, "FAIL: a business that already has colors should never trigger a fresh resolution call"
    print("PASS: reused the already-persisted colors, no re-resolution\n")

    print("=" * 60)
    print("TEST 6: a business with ALREADY-stored colors is a complete no-op")
    print("=" * 60)
    with get_session() as db:
        biz2 = Business(phone="919999999971", name="Colored Biz", industry="salon", onboarding_state="done")
        db.add(biz2)
        db.flush()
        biz2_id = biz2.id

    ctx_colored = BusinessContext(name="Colored Biz", industry="salon", primary_color="#FF00FF", secondary_color="#00FFFF")
    resolved_ctx3 = await orch._resolve_and_lock_colors(biz2_id, ctx_colored)
    assert resolved_ctx3 is ctx_colored, "FAIL: expected the exact same ctx object back (true no-op) when colors are already set"
    with get_session() as db:
        profile2 = db.query(BrandProfile).filter(BrandProfile.business_id == biz2_id).first()
        assert profile2 is None, "FAIL: no BrandProfile row should have been created for a business that never needed color resolution"
    print("PASS: complete no-op for a business that already has colors\n")


async def test_carousel_slides_share_resolved_colors():
    from app.db import get_session
    from app.models import Business
    from app.engine import image_gen, quality, caption as caption_engine, ai_metadata
    from app.whatsapp import client as wa_client

    print("=" * 60)
    print("TEST 7: within ONE carousel, every slide's prompt_builder.build() call sees the SAME resolved colors")
    print("=" * 60)

    sent = []

    async def fake_send_text(to, body):
        sent.append(body)

    async def fake_send_image(to, url, caption=None):
        sent.append(f"[IMG] {url}")

    wa_client.send_text = fake_send_text
    wa_client.send_image = fake_send_image
    orch.send_text = fake_send_text
    orch.send_image = fake_send_image

    async def fake_create_message_picks_once(**kwargs):
        return _FakeResponse('{"primary_color": "#87CEEB", "secondary_color": "#FFFFFF"}')

    prompt_builder.create_message = fake_create_message_picks_once

    async def fake_content_policy_check(text):
        return {"allowed": True, "reason": None}

    orch.content_policy.check = fake_content_policy_check

    async def fake_composite_headline(image_bytes, headline, subtext=None, cta_text=None, language=None):
        return image_bytes

    orch.text_overlay.composite_headline = fake_composite_headline

    seen_colors = []
    real_build = prompt_builder.build

    async def spying_build(ctx, brief):
        seen_colors.append((ctx.primary_color, ctx.secondary_color))
        return {
            "image_prompt": f"a scene: {brief}", "headline_text": "Hi",
            "subtext_text": "", "cta_text": "", "notes_for_caption": brief,
        }

    orch.prompt_builder.build = spying_build

    def _make_png(w=1229, h=1536, color=(10, 10, 10)):
        buf = io.BytesIO()
        Image.new("RGB", (w, h), color).save(buf, format="PNG")
        return buf.getvalue()

    async def fake_generate_images(prompt, count=2, reference_image=None):
        return [_make_png(), _make_png()]

    orch.image_gen.generate_images = fake_generate_images

    async def fake_score_and_pick(images):
        return {"best_index": 0, "best_score": 90, "issues": []}

    orch.quality.score_and_pick = fake_score_and_pick

    async def fake_caption_generate(ctx, brief):
        return {"caption": "a caption", "hashtags": "#tag"}

    orch.caption_engine.generate = fake_caption_generate
    orch.ai_metadata.embed_ai_source_metadata = lambda img: img

    async def fake_upload_to_thread(*args, **kwargs):
        return "https://fake.example.com/x.png"

    orch.upload_creative = lambda *a, **k: "https://fake.example.com/creative.png"
    orch.upload_base_image = lambda *a, **k: "https://fake.example.com/base.png"
    orch.upload_carousel_slide = lambda *a, **k: "https://fake.example.com/slide.png"

    with get_session() as db:
        biz = Business(phone="919999999972", name="Carousel Biz", industry="restaurant", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id

    ctx = BusinessContext(name="Carousel Biz", industry="restaurant")

    try:
        await orch.generate_carousel(
            biz_id, "919999999972", ctx,
            slide_briefs=["Slide 1 content", "Slide 2 content", "Slide 3 content"],
            user_message="make me a 3 slide carousel",
        )
    finally:
        orch.prompt_builder.build = real_build

    assert len(seen_colors) == 3, f"FAIL: expected 3 prompt_builder.build() calls (one per slide), got {len(seen_colors)}"
    assert len(set(seen_colors)) == 1, (
        f"FAIL: every slide must see the SAME resolved colors, got different values across slides: {seen_colors}"
    )
    assert seen_colors[0] == ("#87CEEB", "#FFFFFF"), f"FAIL: expected the resolved palette, got {seen_colors[0]}"
    print(f"PASS: all 3 slides shared identical colors {seen_colors[0]}\n")


async def run():
    await test_resolve_colors_unit()
    await test_resolve_and_lock_colors_persists()
    await test_carousel_slides_share_resolved_colors()
    print("ALL TESTS PASSED")


asyncio.run(run())
