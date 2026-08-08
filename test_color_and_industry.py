"""
Tests for app/engine/color_discovery.py, app/engine/industry_research.py,
and their onboarding.py integration.

Part A: color_discovery.extract_colors_from_image() — real API failure
  (fake key) fails safe to None, never crashes.
Part B: industry_research — skips "other", skips if already cached, real
  API failure logs and swallows rather than raising.
Part C: full onboarding walk — new -> name -> industry (fires research
  task) -> logo (skip) -> color screenshot (mocked confident extraction)
  -> color confirm YES -> tone -> done. A second walk covers the NO branch
  falling through to manual hex entry.
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
    print("PART C: full onboarding walk with mocked color extraction")
    print("=" * 60)

    from app.whatsapp import client as wa_client
    sent = []

    async def fake_send_text(to, body):
        sent.append(("text", body))

    async def fake_send_buttons(to, body, buttons):
        sent.append(("buttons", body, buttons))

    async def fake_download_media(media_id):
        return b"fake screenshot bytes"

    wa_client.send_text = fake_send_text
    wa_client.send_buttons = fake_send_buttons
    wa_client.download_media = fake_download_media

    from app import onboarding
    onboarding.send_text = fake_send_text
    onboarding.send_buttons = fake_send_buttons
    onboarding.download_media = fake_download_media

    async def fake_detect(text):
        return "en"

    async def fake_t(key, language, english_text, **kwargs):
        return english_text.format(**kwargs) if kwargs else english_text

    onboarding.i18n.detect_language = fake_detect
    onboarding.i18n.t = fake_t

    async def fake_upload_logo(business_id, image_bytes):
        return "https://fake-cdn.example.com/logo.png"

    onboarding.upload_logo = fake_upload_logo

    research_calls = []

    async def fake_research(industry):
        research_calls.append(industry)

    onboarding.industry_research.research_and_cache_if_needed = fake_research

    async def fake_extract_confident(image_bytes, media_type="image/jpeg"):
        return {"primary_color": "#1A1A2E", "secondary_color": "#EAB308", "confident": True}

    onboarding.color_discovery.extract_colors_from_image = fake_extract_confident

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
    assert state() == "awaiting_name", f"FAIL: {state()}"

    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="Test Bakery"))
    assert state() == "awaiting_industry", f"FAIL: {state()}"

    sent.clear()
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="button", button_id="salon", text="Salon/Beauty"))
    await asyncio.sleep(0.05)
    assert state() == "awaiting_logo", f"FAIL: {state()}"
    assert research_calls == ["salon"], f"FAIL: expected industry research fired for 'salon', got {research_calls}"
    print("PASS: industry selection correctly fired the background research task\n")

    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="skip"))
    assert state() == "awaiting_color_screenshot", f"FAIL: {state()}"
    print("PASS: logo skip correctly advanced to color screenshot request\n")

    sent.clear()
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="image", media_id="fake_media_123"))
    assert state() == "awaiting_color_confirm", f"FAIL: {state()}"
    assert colors() == ("#1A1A2E", "#EAB308"), f"FAIL: expected extracted colors saved immediately, got {colors()}"
    assert any(kind == "buttons" for kind, *_ in sent), f"FAIL: expected a confirm-buttons message, got {sent}"
    print(f"PASS: confident color extraction saved colors and asked for confirmation: {colors()}\n")

    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="button", button_id="yes_colors", text="Yes, that's right"))
    assert state() == "awaiting_tone", f"FAIL: expected YES to advance straight to tone, got {state()}"
    assert colors() == ("#1A1A2E", "#EAB308"), "FAIL: colors should remain the extracted ones after confirming YES"
    print("PASS: confirming YES kept the extracted colors and advanced to tone\n")

    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="button", button_id="premium", text="Premium"))
    assert state() == "done", f"FAIL: {state()}"
    print("PASS: full flow reached 'done'\n")

    print("--- Second walk: NO branch falls through to manual hex entry ---")
    phone2 = "919999999984"
    with get_session() as db:
        biz2 = Business(phone=phone2, onboarding_state="awaiting_color_screenshot", industry="restaurant")
        db.add(biz2)
        db.flush()
        biz2_id = biz2.id
        db.add(BrandProfile(business_id=biz2_id))

    await onboarding.advance(biz2_id, IncomingMessage(sender=phone2, type="image", media_id="fake_media_456"))
    with get_session() as db:
        assert db.query(Business).filter(Business.id == biz2_id).first().onboarding_state == "awaiting_color_confirm"

    await onboarding.advance(biz2_id, IncomingMessage(sender=phone2, type="button", button_id="no_colors", text="No, let me specify"))
    with get_session() as db:
        biz2_row = db.query(Business).filter(Business.id == biz2_id).first()
        assert biz2_row.onboarding_state == "awaiting_color_manual", f"FAIL: expected manual fallback state, got {biz2_row.onboarding_state}"
    print("PASS: rejecting extracted colors correctly fell through to the manual hex-code question\n")

    await onboarding.advance(biz2_id, IncomingMessage(sender=phone2, type="text", text="#FF5733"))
    with get_session() as db:
        biz2_row = db.query(Business).filter(Business.id == biz2_id).first()
        profile2 = db.query(BrandProfile).filter(BrandProfile.business_id == biz2_id).first()
        assert biz2_row.onboarding_state == "awaiting_tone", f"FAIL: {biz2_row.onboarding_state}"
        assert profile2.primary_color == "#FF5733", f"FAIL: manual hex not saved, got {profile2.primary_color}"
    print("PASS: manually-entered hex code correctly overrode the (rejected) extracted color\n")

    print("ALL TESTS PASSED")


async def run():
    await part_a()
    await part_b()
    await part_c()


asyncio.run(run())
