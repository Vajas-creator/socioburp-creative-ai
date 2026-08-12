"""
Test for app/instagram.py's handle_post_request().

Covers the fix for "Instagram posting fails silently": Make.com's webhook
acks (2xx) the instant it receives the request, normally BEFORE the rest
of the scenario -- including the actual Instagram Graph API publish step
-- has run, so a 2xx response was previously treated as proof of a
successful post when it only proves Make received the request. Fixed by
(a) always logging the full response, not just on failure, so a future
silent-failure report is actually diagnosable, and (b) not claiming
"Posted ✅" for something we can't verify.

Also covers the existing (unchanged) branches: creative not found, already
posted, Instagram not connected, webhook not configured, and a >=400
response from Make.

All Claude/DB/WhatsApp calls mocked -- this is a control-flow test.
"""
import sys
import asyncio
import os
import logging
import uuid

sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_instagram_posting.db")
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
os.environ.setdefault("MAKE_INSTAGRAM_WEBHOOK_URL", "https://hook.eu1.make.com/fake-scenario")

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


from app import db as db_module  # noqa: E402
import app.models  # noqa: E402
db_module.Base.metadata.create_all(bind=db_module.engine)

from app.db import get_session  # noqa: E402
from app.models import Business, Generation  # noqa: E402
from app import instagram  # noqa: E402
from app.engine import learning  # noqa: E402

sent = []


async def fake_send_text(to, body):
    sent.append(body)


instagram.send_text = fake_send_text


async def fake_record_accepted_direction(business_id, generation_id, require_quality_threshold=True):
    pass


learning.record_accepted_direction = fake_record_accepted_direction
instagram.learning.record_accepted_direction = fake_record_accepted_direction

log_records = []


class _ListHandler(logging.Handler):
    def emit(self, record):
        log_records.append((record.levelname, record.getMessage()))


instagram.logger.addHandler(_ListHandler())
instagram.logger.setLevel(logging.INFO)


class _FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


def _make_business_and_generation(phone, instagram_account_id="ig-acct-123", posted=False, carousel_image_urls=None):
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", onboarding_state="done", instagram_account_id=instagram_account_id)
        db.add(biz)
        db.flush()
        biz_id = biz.id
        gen = Generation(
            business_id=biz_id, user_message="make a post", status="done",
            image_url="https://fake.example.com/img.png", caption="Great offer!",
            hashtags="#offer", posted_to_instagram=posted,
            carousel_image_urls=carousel_image_urls,
        )
        db.add(gen)
        db.flush()
        return biz_id, gen.id


async def run():
    print("=" * 60)
    print("TEST 1: successful Make webhook response -> honest copy, full response logged")
    print("=" * 60)
    sent.clear()
    log_records.clear()
    biz_id, gen_id = _make_business_and_generation("919999999940")

    async def fake_post_success(self, url, json=None, **kwargs):
        return _FakeResponse(200, '{"status":"accepted"}')

    import httpx
    real_post = httpx.AsyncClient.post
    httpx.AsyncClient.post = fake_post_success

    await instagram.handle_post_request(biz_id, "919999999940", gen_id)

    httpx.AsyncClient.post = real_post

    assert len(sent) == 1, f"FAIL: expected exactly one message sent, got {sent}"
    assert "Posted to Instagram" not in sent[0] and "✅" not in sent[0], (
        f"FAIL: should not assert unverified success, got {sent[0]!r}"
    )
    assert "Sent to Instagram" in sent[0], f"FAIL: expected the honest confirmation copy, got {sent[0]!r}"
    print(f"PASS: honest confirmation copy used: {sent[0]!r}")

    info_logs = [msg for level, msg in log_records if level == "INFO" and "Make IG webhook response" in msg]
    assert len(info_logs) == 1, f"FAIL: expected the full response logged on the success path, got {log_records}"
    assert '"status":"accepted"' in info_logs[0], f"FAIL: expected the response body in the log, got {info_logs[0]!r}"
    print(f"PASS: full Make response logged even on a 2xx: {info_logs[0]!r}\n")

    with get_session() as db:
        gen = db.query(Generation).filter(Generation.id == gen_id).first()
        assert gen.posted_to_instagram is True
    print("PASS: posted_to_instagram marked True after a successful handoff\n")

    print("=" * 60)
    print("TEST 2: already posted -> short-circuits, no webhook call")
    print("=" * 60)
    sent.clear()
    biz_id2, gen_id2 = _make_business_and_generation("919999999941", posted=True)

    await instagram.handle_post_request(biz_id2, "919999999941", gen_id2)

    assert len(sent) == 1 and "already posted" in sent[0].lower(), f"FAIL: expected already-posted message, got {sent}"
    print(f"PASS: {sent[0]!r}\n")

    print("=" * 60)
    print("TEST 3: business has no instagram_account_id -> not-connected message")
    print("=" * 60)
    sent.clear()
    biz_id3, gen_id3 = _make_business_and_generation("919999999942", instagram_account_id=None)

    await instagram.handle_post_request(biz_id3, "919999999942", gen_id3)

    assert len(sent) == 1 and "isn't connected" in sent[0], f"FAIL: expected not-connected message, got {sent}"
    print(f"PASS: {sent[0]!r}\n")

    print("=" * 60)
    print("TEST 4: Make webhook returns >=400 -> failure message, error logged, NOT marked posted")
    print("=" * 60)
    sent.clear()
    log_records.clear()
    biz_id4, gen_id4 = _make_business_and_generation("919999999943")

    async def fake_post_failure(self, url, json=None, **kwargs):
        return _FakeResponse(500, "internal error")

    httpx.AsyncClient.post = fake_post_failure
    await instagram.handle_post_request(biz_id4, "919999999943", gen_id4)
    httpx.AsyncClient.post = real_post

    assert len(sent) == 1 and "failed" in sent[0].lower(), f"FAIL: expected a failure message, got {sent}"
    error_logs = [msg for level, msg in log_records if level == "ERROR"]
    assert len(error_logs) >= 1, f"FAIL: expected an ERROR log for the failed webhook call, got {log_records}"
    with get_session() as db:
        gen = db.query(Generation).filter(Generation.id == gen_id4).first()
        assert gen.posted_to_instagram is False, "FAIL: must not mark posted_to_instagram on a failed webhook call"
    print(f"PASS: failure handled correctly: {sent[0]!r}, logged: {error_logs}\n")

    print("=" * 60)
    print("TEST 5: a carousel generation (carousel_image_urls set) posts through the 'carousel' branch, not 'photo'")
    print("=" * 60)
    sent.clear()
    log_records.clear()
    slide_urls = [
        "https://fake.example.com/creatives/gen5_slide1.png",
        "https://fake.example.com/creatives/gen5_slide2.png",
        "https://fake.example.com/creatives/gen5_slide3.png",
    ]
    biz_id5, gen_id5 = _make_business_and_generation("919999999944", carousel_image_urls=slide_urls)

    captured_payloads = []

    async def fake_post_capture(self, url, json=None, **kwargs):
        captured_payloads.append(json)
        return _FakeResponse(200, '{"status":"accepted"}')

    httpx.AsyncClient.post = fake_post_capture
    await instagram.handle_post_request(biz_id5, "919999999944", gen_id5)
    httpx.AsyncClient.post = real_post

    assert len(captured_payloads) == 1, f"FAIL: expected exactly one webhook call, got {captured_payloads}"
    payload = captured_payloads[0]
    assert payload["content_type"] == "carousel", f"FAIL: expected content_type='carousel', got {payload}"
    assert "image_url" not in payload, f"FAIL: a carousel payload should not carry a top-level image_url, got {payload}"
    assert payload["files"] == [
        {"media_type": "IMAGE", "image_url": u} for u in slide_urls
    ], f"FAIL: expected files as [{{'media_type': 'IMAGE', 'image_url': ...}}, ...] matching the Make scenario's CreateCarouselPhoto module, got {payload['files']}"
    print(f"PASS: carousel generation posted with content_type='carousel' and a correctly-shaped files array: {payload['files']}\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
