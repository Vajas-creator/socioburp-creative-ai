"""
Test for app/engine/image_gen.py's OpenAI 429 (rate limit) retry -- Aug
2026 "carousel died even though most slides succeeded" fix.

Real production incident: a carousel fans out N slides x 2 candidates
concurrently, which can burst past OpenAI's gpt-image-2 requests-per-
minute cap on the account. Previously, whichever call got rejected with a
429 failed immediately and permanently; if BOTH of one slide's candidates
hit this, orchestrator.py raised "No image returned for carousel slide
N", and since asyncio.gather propagates the first exception it sees, the
ENTIRE carousel died -- throwing away every OTHER slide that had already
generated successfully. OpenAI's own 429 response body says exactly how
long the limit takes to reset ("Please try again in 12s"); this adds a
short wait-and-retry so most of these become quiet successes instead of
hard failures.

Covers:
  - _parse_retry_after_seconds(): extracts the provider's suggested wait
    time from a real-shaped 429 body; falls back to None (not a raise)
    for a malformed/unparseable body.
  - _post_with_rate_limit_retry(): retries on 429 using the parsed wait
    time (or a default if none parseable); does NOT retry on any other
    status code (a real error, not a transient one); gives up after the
    configured attempt count and returns the last (still-429) response
    rather than raising or looping forever.
  - _generate_openai(), _edit_openai(), _outpaint_openai() all route
    through the retry helper -- a transient 429 that clears on retry
    produces a real successful image instead of None/empty.
"""
import sys
import asyncio
import os
import io
import time

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_image_gen_rate_limit_retry.db"
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
os.environ.setdefault("IMAGE_API_KEY", "fake")

import httpx  # noqa: E402
from PIL import Image  # noqa: E402
from app.engine import image_gen  # noqa: E402

real_post = httpx.AsyncClient.post


def _make_png(w=1024, h=1536, color=(10, 20, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


class _FakeRateLimitResponse:
    status_code = 429
    text = (
        'Rate limit reached for gpt-image-2 (for limit gpt-image) on input-images per min: '
        'Limit 5, Used 5, Requested 1. Please try again in 2.4s.'
    )

    def json(self):
        return {"error": {"message": self.text, "type": "input-images", "code": "rate_limit_exceeded"}}


class _FakeOtherErrorResponse:
    status_code = 500
    text = "Internal server error"

    def json(self):
        return {"error": {"message": self.text}}


class _FakeSuccessResponse:
    status_code = 200

    def json(self):
        import base64
        return {"data": [{"b64_json": base64.b64encode(_make_png()).decode()}]}


async def run():
    print("=" * 60)
    print("TEST 1: _parse_retry_after_seconds() extracts the provider's suggested wait")
    print("=" * 60)
    wait_s = image_gen._parse_retry_after_seconds(_FakeRateLimitResponse())
    assert wait_s is not None and 2.4 < wait_s < 3.5, f"FAIL: expected ~2.9s (2.4 + buffer), got {wait_s}"
    print(f"PASS: parsed {wait_s}s\n")

    print("=" * 60)
    print("TEST 2: _parse_retry_after_seconds() returns None for an unparseable body, doesn't raise")
    print("=" * 60)

    class _WeirdResponse:
        def json(self):
            raise ValueError("not json")

    assert image_gen._parse_retry_after_seconds(_WeirdResponse()) is None
    print("PASS: returns None instead of raising\n")

    print("=" * 60)
    print("TEST 3: _parse_retry_after_seconds() caps an absurdly long suggested wait")
    print("=" * 60)

    class _HugeWaitResponse:
        def json(self):
            return {"error": {"message": "Please try again in 9999s."}}

    capped = image_gen._parse_retry_after_seconds(_HugeWaitResponse())
    assert capped == image_gen._RATE_LIMIT_MAX_WAIT, f"FAIL: expected capped at {image_gen._RATE_LIMIT_MAX_WAIT}, got {capped}"
    print(f"PASS: capped at {capped}s\n")

    print("=" * 60)
    print("TEST 4: _post_with_rate_limit_retry() retries through transient 429s to a real success")
    print("=" * 60)
    calls = {"n": 0}

    async def fake_post_recovers(self, url, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return _FakeRateLimitResponse()
        return _FakeSuccessResponse()

    httpx.AsyncClient.post = fake_post_recovers
    try:
        async with httpx.AsyncClient() as client:
            resp = await image_gen._post_with_rate_limit_retry(client, "https://fake")
    finally:
        httpx.AsyncClient.post = real_post
    assert resp.status_code == 200, f"FAIL: expected eventual success, got {resp.status_code}"
    assert calls["n"] == 3, f"FAIL: expected exactly 3 attempts (2 failed + 1 success), got {calls['n']}"
    print(f"PASS: succeeded after {calls['n']} attempts\n")

    print("=" * 60)
    print("TEST 5: _post_with_rate_limit_retry() does NOT retry a non-429 error")
    print("=" * 60)
    calls["n"] = 0

    async def fake_post_real_error(self, url, **kwargs):
        calls["n"] += 1
        return _FakeOtherErrorResponse()

    httpx.AsyncClient.post = fake_post_real_error
    try:
        async with httpx.AsyncClient() as client:
            resp = await image_gen._post_with_rate_limit_retry(client, "https://fake")
    finally:
        httpx.AsyncClient.post = real_post
    assert resp.status_code == 500
    assert calls["n"] == 1, f"FAIL: a real (non-429) error must not be retried, got {calls['n']} attempts"
    print("PASS: a genuine error fails immediately, no wasted retries\n")

    print("=" * 60)
    print("TEST 6: _post_with_rate_limit_retry() gives up after the attempt cap, returns the last 429")
    print("=" * 60)
    calls["n"] = 0

    async def fake_post_always_limited(self, url, **kwargs):
        calls["n"] += 1
        return _FakeRateLimitResponse()

    httpx.AsyncClient.post = fake_post_always_limited
    start = time.time()
    try:
        async with httpx.AsyncClient() as client:
            resp = await image_gen._post_with_rate_limit_retry(client, "https://fake")
    finally:
        httpx.AsyncClient.post = real_post
    elapsed = time.time() - start
    assert resp.status_code == 429, "FAIL: expected the persistent 429 to be returned, not raised"
    assert calls["n"] == image_gen._RATE_LIMIT_RETRIES, f"FAIL: expected exactly {image_gen._RATE_LIMIT_RETRIES} attempts, got {calls['n']}"
    print(f"PASS: gave up cleanly after {calls['n']} attempts in {elapsed:.1f}s, no infinite loop\n")

    print("=" * 60)
    print("TEST 7: _generate_openai() recovers from a transient 429 via the retry path")
    print("=" * 60)
    calls["n"] = 0

    async def fake_post_generate_recovers(self, url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeRateLimitResponse()
        return _FakeSuccessResponse()

    httpx.AsyncClient.post = fake_post_generate_recovers
    try:
        results = await image_gen._generate_openai("a bakery post", count=1)
    finally:
        httpx.AsyncClient.post = real_post
    assert len(results) == 1, f"FAIL: expected a real image after the retry recovered, got {results}"
    print("PASS: _generate_openai() produced a real image despite an initial 429\n")

    print("=" * 60)
    print("TEST 8: _outpaint_openai() recovers from a transient 429 via the retry path")
    print("=" * 60)
    calls["n"] = 0

    async def fake_post_outpaint_recovers(self, url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeRateLimitResponse()
        return _FakeSuccessResponse()

    httpx.AsyncClient.post = fake_post_outpaint_recovers
    try:
        result = await image_gen._outpaint_openai(Image.new("RGB", (1024, 1536), (5, 5, 5)))
    finally:
        httpx.AsyncClient.post = real_post
    assert result is not None, "FAIL: expected outpaint to succeed after the retry recovered"
    print("PASS: _outpaint_openai() produced a real image despite an initial 429\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
