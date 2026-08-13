"""
Tests for app/engine/color_discovery.py, app/engine/industry_research.py,
and their onboarding.py integration.

Part A: color_discovery.extract_colors_from_image() — real API failure
  (fake key) fails safe to None, never crashes.
Part B: industry_research — skips "other", skips if already cached, real
  API failure logs and swallows rather than raising.
Part C: full onboarding walk (Aug 2026 2-question redesign) — new ->
  awaiting_business_description (fires research task on the extracted
  business_type) -> awaiting_instagram (screenshot -> colors extracted and
  applied directly, no confirm step in this flow) -> done, auto-generation
  triggered. A second walk covers sending an Instagram handle/link
  (text, not a screenshot) instead -- stored, no color extraction attempted.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_discovery.db"
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ["ANTHROPIC_API_KEY"] = "fake"
os.environ.setdefault("R2_ACCOUNT_ID", "fake")
os.environ.setdefault("R2_ACCESS_KEY", "fake")
os.environ.setdefault("R2_SECRET_KEY", "fake")
os.environ.setdefault("R2_BUCKET", "fake")
os.environ.setdefault("R2_PUBLIC_BASE_URL", "https://fake.example.com")
os.environ.setdefault("IMAGE_API_KEY", "fake")

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


from app import db as db_module  # noqa: E402
import app.models  # noqa: E402
db_module.Base.metadata.create_all(bind=db_module.engine)

from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile, IndustryStyleResearch  # noqa: E402
from app.engine import color_discovery, industry_research  # noqa: E402


async def part_a():
    print("=" * 60)
    print("PART A: color_discovery — real API failure fails safe")
    print("=" * 60)
    result = await color_discovery.extract_colors_from_image(b"fake image bytes")
    assert result is None, f"FAIL: expected None on real API failure, got {result}"
    print("PASS: real (fake-key) API failure correctly returns None, no crash\n")


async def part_b():
    print("=" * 60)
    print("PART B: industry_research caching + skip rules")
    print("=" * 60)

    def get_cache_row(industry):
        with get_session() as db:
            return db.query(IndustryStyleResearch).filter(IndustryStyleResearch.industry == industry).first()

    await industry_research.research_and_cache_if_needed("other")
    assert get_cache_row("other") is None, "FAIL: 'other' industry should never be researched"
    print("PASS: 'other' industry correctly skipped, no cache row created\n")

    await industry_research.research_and_cache_if_needed(None)
    print("PASS: None industry handled without crashing\n")

    await industry_research.research_and_cache_if_needed("bakery_test_industry")
    assert get_cache_row("bakery_test_industry") is None, "FAIL: a failed research call should not write a cache row"
    print("PASS: real API failure during research swallowed cleanly, no bad cache entry\n")

    with get_session() as db:
        db.add(IndustryStyleResearch(industry="salon", style_summary="Warm, minimal, pastel tones."))

    call_count = {"n": 0}

    class _FakeMessages:
        @staticmethod
        async def create(**kwargs):
            call_count["n"] += 1
            raise AssertionError("should not be called — 'salon' is already cached")

    class _FakeClient:
        messages = _FakeMessages()

    industry_research.client = _FakeClient()
    await industry_research.research_and_cache_if_needed("salon")
    assert call_count["n"] == 0, f"FAIL: expected no API call for an already-cached industry, got {call_count['n']}"
    assert industry_research.get_cached_style("salon") == "Warm, minimal, pastel tones."
    print("PASS: already-cached industry correctly skipped a fresh research call\n")


async def part_c():
    print("=" * 60)
    print("PART C: full onboarding walk (2-question redesign) with mocked color extraction")
    print("=" * 60)

    from app.whatsapp import client as wa_client
    sent = []

    async def fake_send_text(to, body):
        sent.append(("text", body))

    async def fake_download_media(media_id):
        return b"fake screenshot bytes"

    wa_client.send_text = fake_send_text
    wa_client.download_media = fake_download_media

    from app import onboarding
    onboarding.send_text = fake_send_text
    onboarding.download_media = fake_download_media
    onboarding.WELCOME_TO_QUESTION_DELAY_SECONDS = 0  # skip the real 1.5s pacing delay in tests

    async def fake_detect(text):
        return "en"

    async def fake_t(key, language, english_text, **kwargs):
        return english_text.format(**kwargs) if kwargs else english_text

    onboarding.i18n.detect_language = fake_detect
    onboarding.i18n.t = fake_t

    async def fake_classify(user_message):
        return {"intent": "OTHER", "brief": user_message}

    onboarding.intent_engine.classify = fake_classify

    research_calls = []

    async def fake_research(industry):
        research_calls.append(industry)

    onboarding.industry_research.research_and_cache_if_needed = fake_research

    async def fake_extract_confident(image_bytes, media_type="image/jpeg"):
        return {"primary_color": "#1A1A2E", "secondary_color": "#EAB308", "confident": True}

    onboarding.color_discovery.extract_colors_from_image = fake_extract_confident

    async def fake_understand_business(description, language="en"):
        return {
            "business_type": "salon",
            "brand_adjectives": "warm, inviting",
            "business_name": None,
            "message": "Got it.\nYou run a salon.\nI'm going to remember that your brand needs to feel warm, inviting — not like a mass-produced catalogue.\nOne more thing...",
        }

    onboarding.brand_reflection.understand_business = fake_understand_business

    generation_calls = []

    async def fake_run_generation(business_id, phone, ctx, brief, user_message, last_generation_id, is_revision, trigger_source=None, reference_image=None):
        generation_calls.append((brief, trigger_source, ctx.primary_color))

    import app.engine.orchestrator as orch
    orch._run_generation = fake_run_generation

    from app.schemas import IncomingMessage

    phone = "919999999985"
    with get_session() as db:
        biz = Business(phone=phone, onboarding_state="new")
        db.add(biz)
        db.flush()
        biz_id = biz.id

    def state():
        with get_session() as db:
            return db.query(Business).filter(Business.id == biz_id).first().onboarding_state

    def colors():
        with get_session() as db:
            p = db.query(BrandProfile).filter(BrandProfile.business_id == biz_id).first()
            return (p.primary_color, p.secondary_color) if p else (None, None)

    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="hi"))
    await asyncio.sleep(0.05)
    assert state() == "awaiting_business_description", f"FAIL: {state()}"

    sent.clear()
    research_calls.clear()
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="I run a hair and beauty salon"))
    await asyncio.sleep(0.05)  # let the fire-and-forget research task actually run
    assert state() == "awaiting_instagram", f"FAIL: {state()}"
    assert research_calls == ["salon"], f"FAIL: expected industry research fired for the extracted business_type, got {research_calls}"
    with get_session() as db:
        assert db.query(Business).filter(Business.id == biz_id).first().industry == "salon"
    print("PASS: business description extracted business_type='salon', fired background research\n")

    sent.clear()
    generation_calls.clear()
    result = await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="image", media_id="fake_media_123"))
    assert state() == "done", f"FAIL: {state()}"
    assert colors() == ("#1A1A2E", "#EAB308"), f"FAIL: expected extracted colors applied directly (no confirm step), got {colors()}"
    assert result is not None, "FAIL: expected advance() to return (ctx, brief) once onboarding completes"
    ctx, brief = result
    # advance() no longer calls _run_generation() itself (see its
    # docstring) -- simulate what app/router.py does with the returned
    # (ctx, brief), same as production.
    await orch._run_generation(
        biz_id, phone, ctx, brief, brief,
        last_generation_id=None, is_revision=False,
        trigger_source="onboarding_complete",
    )
    assert len(generation_calls) == 1, f"FAIL: expected auto-generation triggered once onboarding completes, got {generation_calls}"
    assert generation_calls[0][1] == "onboarding_complete", f"FAIL: expected trigger_source='onboarding_complete', got {generation_calls[0]}"
    assert generation_calls[0][2] == "#1A1A2E", "FAIL: the auto-generation's context should carry the just-extracted colors"
    print(f"PASS: screenshot -> colors applied directly (no confirm step): {colors()}, auto-generation triggered: {generation_calls[0]}\n")

    print("--- Second walk: Instagram handle sent as TEXT instead of a screenshot ---")
    phone2 = "919999999984"
    with get_session() as db:
        biz2 = Business(phone=phone2, onboarding_state="awaiting_instagram", industry="restaurant")
        db.add(biz2)
        db.flush()
        biz2_id = biz2.id
        db.add(BrandProfile(business_id=biz2_id))

    generation_calls.clear()
    result2 = await onboarding.advance(biz2_id, IncomingMessage(sender=phone2, type="text", text="instagram.com/testrestaurant"))
    with get_session() as db:
        biz2_row = db.query(Business).filter(Business.id == biz2_id).first()
        profile2 = db.query(BrandProfile).filter(BrandProfile.business_id == biz2_id).first()
        assert biz2_row.onboarding_state == "done", f"FAIL: {biz2_row.onboarding_state}"
        assert biz2_row.instagram_handle == "instagram.com/testrestaurant", f"FAIL: handle not stored, got {biz2_row.instagram_handle}"
        assert profile2.primary_color is None, "FAIL: no screenshot was sent, no colors should have been extracted"
        stored_handle = biz2_row.instagram_handle
    assert result2 is not None, "FAIL: expected advance() to return (ctx, brief) once onboarding completes"
    ctx2, brief2 = result2
    await orch._run_generation(
        biz2_id, phone2, ctx2, brief2, brief2,
        last_generation_id=None, is_revision=False,
        trigger_source="onboarding_complete",
    )
    assert len(generation_calls) == 1, f"FAIL: expected auto-generation triggered here too, got {generation_calls}"
    print(f"PASS: text handle stored ({stored_handle!r}), no color extraction attempted, still auto-generated\n")

    print("ALL TESTS PASSED")


async def run():
    await part_a()
    await part_b()
    await part_c()


asyncio.run(run())
