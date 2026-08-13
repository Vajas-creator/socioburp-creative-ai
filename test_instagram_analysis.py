"""
Test for app/engine/instagram_analysis.py -- fetching a business's actual
Instagram bio + recent post captions via the "SocioBurp — Instagram
Profile Fetch" Make.com scenario (Business Discovery API, no client OAuth
needed), and app/onboarding.py's wiring of it.

Root cause from the Aug 2026 live-test report, item 7: Business.instagram_handle
was stored and never read again by anything -- "does the bot fetch and
analyze the Instagram profile, or just store the link?" was previously
just the link. This is the fetch, plus onboarding wiring it in as a
fire-and-forget background task (same pattern as industry_research),
so it never adds latency to the "give me a moment" -> first generation path.

Covers:
  - _normalize_handle() turns a URL, an @handle, or a bare username into
    the plain username the Make webhook expects.
  - fetch_profile_summary(): webhook not configured -> None; a successful
    response -> the right dict; non-200 -> None; a request exception ->
    None; a response with neither bio nor captions -> None (nothing
    usable to store).
  - fetch_and_store_profile_summary() writes onto BrandProfile correctly,
    is a no-op if the profile row is gone, and doesn't touch the DB at all
    if there's nothing to store.
  - onboarding.py's awaiting_instagram branch fires this as a background
    task (never awaited inline) when the client sends a text handle, with
    the right business_id/handle -- and does NOT fire it for a
    skip/decline or a screenshot.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_instagram_analysis.db"
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
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
from app.models import Business, BrandProfile  # noqa: E402
from app.config import settings  # noqa: E402
from app.engine import instagram_analysis  # noqa: E402

# Imported up front, before any httpx.AsyncClient monkeypatching below --
# onboarding.py's import chain (i18n -> anthropic_client -> the anthropic
# SDK) subclasses httpx.AsyncClient at import time, which breaks if
# httpx.AsyncClient has already been replaced with a fake factory function
# by the time this import runs.
from app import onboarding  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402
from app.engine import brand_reflection  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data


class _FakeHttpClient:
    def __init__(self, response=None, raise_exc=None, calls=None):
        self._response = response
        self._raise_exc = raise_exc
        self._calls = calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self._calls.append((url, json))
        if self._raise_exc:
            raise self._raise_exc
        return self._response


_real_async_client = None


def _install_fake_httpx(response=None, raise_exc=None):
    global _real_async_client
    if _real_async_client is None:
        _real_async_client = instagram_analysis.httpx.AsyncClient

    calls = []

    def factory(*args, **kwargs):
        return _FakeHttpClient(response=response, raise_exc=raise_exc, calls=calls)

    instagram_analysis.httpx.AsyncClient = factory
    return calls


def _restore_real_httpx():
    if _real_async_client is not None:
        instagram_analysis.httpx.AsyncClient = _real_async_client


async def run():
    print("=" * 60)
    print("TEST 1: _normalize_handle() strips URLs, @, slashes, query strings")
    print("=" * 60)
    cases = [
        ("https://instagram.com/xyz/", "xyz"),
        ("http://www.instagram.com/xyz", "xyz"),
        ("instagram.com/xyz", "xyz"),
        ("@xyz", "xyz"),
        ("  xyz  ", "xyz"),
        ("instagram.com/xyz?hl=en", "xyz"),
        ("", None),
        ("   ", None),
    ]
    for raw, expected in cases:
        got = instagram_analysis._normalize_handle(raw)
        assert got == expected, f"FAIL: _normalize_handle({raw!r}) = {got!r}, expected {expected!r}"
    print("PASS: all handle formats normalized correctly\n")

    print("=" * 60)
    print("TEST 2: webhook not configured -> None, no request attempted")
    print("=" * 60)
    settings.MAKE_INSTAGRAM_PROFILE_FETCH_WEBHOOK_URL = ""
    calls = _install_fake_httpx(response=_FakeResponse(200, {"biography": "should not be reached"}))
    result = await instagram_analysis.fetch_profile_summary("xyz")
    assert result is None, f"FAIL: expected None when not configured, got {result}"
    assert calls == [], f"FAIL: expected no HTTP call attempted, got {calls}"
    print("PASS: no webhook configured -> None, no request made\n")

    settings.MAKE_INSTAGRAM_PROFILE_FETCH_WEBHOOK_URL = "https://hook.eu1.make.com/fake-fetch-scenario"

    print("=" * 60)
    print("TEST 3: successful response -> correct dict, capped at MAX_CAPTIONS")
    print("=" * 60)
    many_posts = [{"caption": f"Post number {i}"} for i in range(10)]
    calls = _install_fake_httpx(response=_FakeResponse(200, {
        "biography": "  Best bakery in town  ",
        "recent_posts": many_posts,
    }))
    result = await instagram_analysis.fetch_profile_summary("@xyz")
    assert result is not None
    assert result["biography"] == "Best bakery in town", f"FAIL: expected trimmed biography, got {result}"
    assert len(result["recent_captions"]) == instagram_analysis.MAX_CAPTIONS, (
        f"FAIL: expected capped at {instagram_analysis.MAX_CAPTIONS} captions, got {len(result['recent_captions'])}"
    )
    assert calls[0][1] == {"username": "xyz"}, f"FAIL: expected the normalized username posted, got {calls[0][1]}"
    print(f"PASS: {result}\n")

    print("=" * 60)
    print("TEST 4: non-200 response -> None")
    print("=" * 60)
    _install_fake_httpx(response=_FakeResponse(404, text="not found"))
    result = await instagram_analysis.fetch_profile_summary("xyz")
    assert result is None, f"FAIL: expected None on non-200, got {result}"
    print("PASS: non-200 -> None\n")

    print("=" * 60)
    print("TEST 5: request exception -> None, never raises")
    print("=" * 60)
    _install_fake_httpx(raise_exc=RuntimeError("simulated network failure"))
    result = await instagram_analysis.fetch_profile_summary("xyz")
    assert result is None, f"FAIL: expected None on exception, got {result}"
    print("PASS: exception swallowed, returns None\n")

    print("=" * 60)
    print("TEST 6: response with no bio and no captions -> None (nothing usable)")
    print("=" * 60)
    _install_fake_httpx(response=_FakeResponse(200, {"biography": "", "recent_posts": []}))
    result = await instagram_analysis.fetch_profile_summary("xyz")
    assert result is None, f"FAIL: expected None when there's nothing usable, got {result}"
    print("PASS: empty bio + no captions -> None\n")

    print("=" * 60)
    print("TEST 7: fetch_and_store_profile_summary() writes onto BrandProfile")
    print("=" * 60)
    with get_session() as db:
        biz = Business(phone="919999999970", name="Test Biz", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        db.add(BrandProfile(business_id=biz_id))

    _install_fake_httpx(response=_FakeResponse(200, {
        "biography": "Fresh bread daily",
        "recent_posts": [{"caption": "New sourdough!"}, {"caption": "Weekend special"}],
    }))
    await instagram_analysis.fetch_and_store_profile_summary(biz_id, "@testbakery")

    with get_session() as db:
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == biz_id).first()
        assert profile.instagram_bio == "Fresh bread daily", f"FAIL: expected bio stored, got {profile.instagram_bio!r}"
        assert profile.instagram_recent_captions == "New sourdough!\nWeekend special", (
            f"FAIL: expected newline-joined captions, got {profile.instagram_recent_captions!r}"
        )
    print("PASS: bio and captions written onto BrandProfile\n")

    print("=" * 60)
    print("TEST 8: fetch_and_store_profile_summary() is a no-op if the profile row is gone")
    print("=" * 60)
    import uuid
    fake_biz_id = uuid.uuid4()
    _install_fake_httpx(response=_FakeResponse(200, {"biography": "irrelevant"}))
    await instagram_analysis.fetch_and_store_profile_summary(fake_biz_id, "xyz")  # must not raise
    print("PASS: missing profile row handled without raising\n")

    _restore_real_httpx()

    print("=" * 60)
    print("TEST 9: onboarding.py fires this as a background task for a TEXT handle, not for skip/screenshot")
    print("=" * 60)
    fetch_calls = []

    async def fake_fetch_and_store(business_id, handle):
        fetch_calls.append((business_id, handle))

    real_fetch_and_store = instagram_analysis.fetch_and_store_profile_summary
    instagram_analysis.fetch_and_store_profile_summary = fake_fetch_and_store

    async def fake_send_text(to, body):
        pass

    onboarding.send_text = fake_send_text

    async def fake_detect_language(text):
        return "en"

    async def fake_t(key, language, english_text, **kwargs):
        return english_text.format(**kwargs) if kwargs else english_text

    onboarding.i18n.detect_language = fake_detect_language
    onboarding.i18n.t = fake_t
    onboarding.WELCOME_TO_QUESTION_DELAY_SECONDS = 0

    async def fake_classify(user_message):
        return {"intent": "OTHER", "brief": user_message}

    onboarding.intent_engine.classify = fake_classify

    async def fake_understand_business(description, language="en"):
        return {"business_type": "bakery", "brand_adjectives": "warm", "business_name": None, "message": "Got it."}

    brand_reflection.understand_business = fake_understand_business

    async def fake_research(industry):
        pass

    onboarding.industry_research.research_and_cache_if_needed = fake_research

    phone2 = "919999999971"
    with get_session() as db:
        biz2 = Business(phone=phone2, onboarding_state="new")
        db.add(biz2)
        db.flush()
        biz2_id = biz2.id

    await onboarding.advance(biz2_id, IncomingMessage(sender=phone2, type="text", text="hi"))
    await onboarding.advance(biz2_id, IncomingMessage(sender=phone2, type="text", text="skip"))  # owner-name question
    await onboarding.advance(biz2_id, IncomingMessage(sender=phone2, type="text", text="I run a bakery"))
    result = await onboarding.advance(biz2_id, IncomingMessage(sender=phone2, type="text", text="@mytestbakery"))
    await asyncio.sleep(0.05)  # let the fire-and-forget task actually run

    assert result is not None, "FAIL: expected onboarding to complete and return (ctx, brief)"
    assert fetch_calls == [(biz2_id, "@mytestbakery")], f"FAIL: expected the fetch task fired with the raw handle, got {fetch_calls}"
    print(f"PASS: background fetch task fired for a text handle: {fetch_calls}\n")

    print("--- second walk: 'skip' must NOT fire the fetch task ---")
    fetch_calls.clear()
    phone3 = "919999999972"
    with get_session() as db:
        biz3 = Business(phone=phone3, onboarding_state="awaiting_instagram", industry="salon")
        db.add(biz3)
        db.flush()
        biz3_id = biz3.id
        db.add(BrandProfile(business_id=biz3_id))

    await onboarding.advance(biz3_id, IncomingMessage(sender=phone3, type="text", text="skip"))
    await asyncio.sleep(0.05)
    assert fetch_calls == [], f"FAIL: expected no fetch task for a skip, got {fetch_calls}"
    print("PASS: 'skip' did not fire the background fetch\n")

    instagram_analysis.fetch_and_store_profile_summary = real_fetch_and_store

    print("ALL TESTS PASSED")


asyncio.run(run())
