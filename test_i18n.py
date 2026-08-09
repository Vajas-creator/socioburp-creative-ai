"""
Tests for app/i18n.py and its onboarding integration.

Part A: detect_language() — real API failure (fake key) falls back to 'en';
  a mocked successful call returns the detected language correctly.
Part B: t() — translates once per (key, language) and caches (proven via
  a call counter on the mocked client, not just correct output); format
  placeholders applied after translation, not before; a translation that
  breaks a placeholder falls back to English rather than crashing.
Part C: onboarding.advance() from state="new" detects language from the
  client's actual first message and stores it; the manual override
  keyword switches it later.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_i18n.db"
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

from app import i18n  # noqa: E402


async def part_a():
    print("=" * 60)
    print("PART A: detect_language()")
    print("=" * 60)

    # Real (fake-key) API failure -> must fail safe to 'en', never crash or block onboarding
    result = await i18n.detect_language("இது ஒரு சோதனை செய்தி")  # real Tamil text, but API call will fail
    assert result == "en", f"FAIL: expected fail-safe 'en' on real API failure, got {result!r}"
    print("PASS: real API failure (fake key) fails safe to 'en'\n")

    assert await i18n.detect_language("") == "en", "FAIL: empty text should be 'en'"
    assert await i18n.detect_language("   ") == "en", "FAIL: whitespace-only text should be 'en'"
    print("PASS: empty/whitespace input short-circuits to 'en' without an API call\n")

    # Now mock a working client to prove correct-path behavior
    class _FakeResponse:
        def __init__(self, text):
            self.content = [type("Block", (), {"text": text})()]

    async def _fake_create_message(**kwargs):
        return _FakeResponse('{"language": "ta"}')

    i18n.create_message = _fake_create_message
    result = await i18n.detect_language("இது ஒரு சோதனை செய்தி")
    assert result == "ta", f"FAIL: expected 'ta' from mocked successful detection, got {result!r}"
    print("PASS: mocked successful detection returns 'ta' correctly\n")


async def part_b():
    print("=" * 60)
    print("PART B: t() translation + caching")
    print("=" * 60)

    call_count = {"n": 0}

    class _FakeResponse:
        def __init__(self, text):
            self.content = [type("Block", (), {"text": text})()]

    async def _fake_create_message(**kwargs):
        call_count["n"] += 1
        return _FakeResponse("வணக்கம்! நீங்கள் {credits} கிரெடிட்களைப் பெற்றுள்ளீர்கள்")

    i18n.create_message = _fake_create_message
    i18n._translation_cache.clear()

    r1 = await i18n.t("greeting_test", "ta", "Welcome! You have {credits} credits", credits=20)
    assert "20" in r1, f"FAIL: expected the credits value substituted, got {r1!r}"
    assert call_count["n"] == 1, f"FAIL: expected 1 API call after first translation, got {call_count['n']}"
    print(f"PASS: first call translates and substitutes correctly: {r1!r}\n")

    r2 = await i18n.t("greeting_test", "ta", "Welcome! You have {credits} credits", credits=5)
    assert "5" in r2 and "20" not in r2, f"FAIL: expected fresh substitution with cached template, got {r2!r}"
    assert call_count["n"] == 1, f"FAIL: expected NO new API call on second use (cached), got {call_count['n']} total calls"
    print(f"PASS: second call reused the cached template, only re-applied formatting: {r2!r} (still 1 API call total)\n")

    r3 = await i18n.t("greeting_test", "en", "Welcome! You have {credits} credits", credits=99)
    assert r3 == "Welcome! You have 99 credits", f"FAIL: English should bypass translation entirely, got {r3!r}"
    assert call_count["n"] == 1, f"FAIL: English path should never call the API, got {call_count['n']} total calls"
    print(f"PASS: English bypasses translation entirely: {r3!r}\n")

    print("--- Broken placeholder fallback ---")

    async def _fake_create_message_broken(**kwargs):
        return _FakeResponse("இது ஒரு மொழிபெயர்ப்பு பிழை")  # no {credits} placeholder at all — broken

    i18n.create_message = _fake_create_message_broken
    r4 = await i18n.t("broken_test", "ta", "You have {credits} credits", credits=7)
    assert r4 == "You have 7 credits", f"FAIL: a translation missing the placeholder should fall back to English, got {r4!r}"
    print(f"PASS: translation that dropped the {{credits}} placeholder correctly fell back to English: {r4!r}\n")


async def part_c():
    print("=" * 60)
    print("PART C: onboarding integration")
    print("=" * 60)

    from app.whatsapp import client as wa_client
    sent = []

    async def fake_send_text(to, body):
        sent.append(body)

    async def fake_send_buttons(to, body, buttons):
        sent.append(body)

    wa_client.send_text = fake_send_text
    wa_client.send_buttons = fake_send_buttons

    from app import onboarding
    onboarding.send_text = fake_send_text
    onboarding.send_buttons = fake_send_buttons

    # Mock detection to deterministically return 'hi' for this test, and
    # translation to a fixed, recognizable string.
    async def fake_detect(text):
        return "hi"

    async def fake_t(key, language, english_text, **kwargs):
        if language == "en":
            return english_text.format(**kwargs) if kwargs else english_text
        return f"[HI:{key}]" + (english_text.format(**kwargs) if kwargs else english_text)

    i18n.detect_language = fake_detect
    i18n.t = fake_t
    onboarding.i18n.detect_language = fake_detect
    onboarding.i18n.t = fake_t
    onboarding.WELCOME_TO_QUESTION_DELAY_SECONDS = 0  # skip the real 1.5s pacing delay in tests

    async def fake_classify(user_message):
        return {"intent": "OTHER", "brief": user_message}

    onboarding.intent_engine.classify = fake_classify

    from app.db import get_session
    from app.models import Business
    from app.schemas import IncomingMessage

    phone = "919999999988"
    with get_session() as db:
        biz = Business(phone=phone, onboarding_state="new")
        db.add(biz)
        db.flush()
        biz_id = biz.id

    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="नमस्ते, मुझे मदद चाहिए"))

    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id).first()
        assert biz.preferred_language == "hi", f"FAIL: expected preferred_language='hi' detected from first message, got {biz.preferred_language!r}"
        assert biz.onboarding_state == "awaiting_business_description", f"FAIL: expected state advanced to awaiting_business_description, got {biz.onboarding_state!r}"

    assert any("[HI:welcome]" in s for s in sent), f"FAIL: expected the translated welcome message sent, got {sent}"
    print(f"PASS: first message in Hindi correctly detected and stored, translated welcome sent: {sent[-1][:60]}...\n")

    print("--- Manual override ---")
    sent.clear()
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="english"))
    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id).first()
        assert biz.preferred_language == "en", f"FAIL: expected manual override to 'en', got {biz.preferred_language!r}"
    print("PASS: manual 'english' override correctly switched preferred_language\n")

    print("ALL TESTS PASSED")


async def run():
    await part_a()
    await part_b()
    await part_c()


asyncio.run(run())
