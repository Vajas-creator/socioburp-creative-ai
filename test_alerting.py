"""
Test for app/alerting.py -- Telegram-based failure alerting (Priority 8 of
the Aug 2026 consolidated fix list). Render's free tier has no shell for
proactively tailing logs, so send_alert() fires immediately from inside
the existing except blocks that already catch unhandled errors and failed
generations, instead of relying on someone noticing a silent log line.

Covers:
  - send_alert() is a silent no-op when Telegram isn't configured (no
    ALERT_TELEGRAM_TOKEN / ALERT_TELEGRAM_CHAT_ID) -- must never raise or
    block the caller just because alerting isn't set up.
  - send_alert() posts to the Telegram Bot API with the expected chat_id
    and a message that includes the `kind` tag and the given text.
  - Per-kind cooldown: a second alert of the SAME kind within
    COOLDOWN_SECONDS is suppressed (no second HTTP call), but a
    DIFFERENT kind alerts immediately regardless of the first kind's
    cooldown.
  - send_alert() never raises even if the Telegram HTTP call itself fails
    (e.g. network error) -- alerting a failure must not become a second,
    unhandled failure.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_alerting.db"
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")

from app import alerting  # noqa: E402
from app.config import settings  # noqa: E402


async def test_noop_when_unconfigured():
    print("=" * 60)
    print("TEST 1: send_alert() is a silent no-op with no Telegram config")
    print("=" * 60)
    settings.ALERT_TELEGRAM_TOKEN = ""
    settings.ALERT_TELEGRAM_CHAT_ID = ""
    alerting._last_sent.clear()

    calls = []

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            calls.append((url, json))

    alerting.httpx.AsyncClient = FakeAsyncClient

    await alerting.send_alert("some_kind", "should not send anywhere")
    assert calls == [], f"FAIL: expected no HTTP call when unconfigured, got {calls}"
    print("PASS: no HTTP call made\n")


async def test_sends_with_expected_payload():
    print("=" * 60)
    print("TEST 2: send_alert() posts to the Telegram Bot API with the right payload")
    print("=" * 60)
    settings.ALERT_TELEGRAM_TOKEN = "fake-token"
    settings.ALERT_TELEGRAM_CHAT_ID = "12345"
    alerting._last_sent.clear()

    calls = []

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            calls.append((url, json))

    alerting.httpx.AsyncClient = FakeAsyncClient

    await alerting.send_alert("generation_failed", "boom, business=abc123")

    assert len(calls) == 1, f"FAIL: expected exactly 1 call, got {calls}"
    url, payload = calls[0]
    assert "fake-token" in url, f"FAIL: expected the token in the URL, got {url}"
    assert payload["chat_id"] == "12345", f"FAIL: expected chat_id 12345, got {payload}"
    assert "generation_failed" in payload["text"], f"FAIL: expected the kind tag in the text, got {payload}"
    assert "boom, business=abc123" in payload["text"], f"FAIL: expected the message in the text, got {payload}"
    print(f"PASS: {payload}\n")


async def test_cooldown_suppresses_same_kind_but_not_different_kind():
    print("=" * 60)
    print("TEST 3: cooldown suppresses a repeat of the SAME kind, not a different kind")
    print("=" * 60)
    settings.ALERT_TELEGRAM_TOKEN = "fake-token"
    settings.ALERT_TELEGRAM_CHAT_ID = "12345"
    alerting._last_sent.clear()

    calls = []

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            calls.append(json["text"])

    alerting.httpx.AsyncClient = FakeAsyncClient

    await alerting.send_alert("kind_a", "first kind_a")
    await alerting.send_alert("kind_a", "second kind_a, should be suppressed")
    await alerting.send_alert("kind_b", "first kind_b, different kind, should NOT be suppressed")

    assert len(calls) == 2, f"FAIL: expected 2 calls (1st kind_a + kind_b), got {len(calls)}: {calls}"
    assert "first kind_a" in calls[0]
    assert "kind_b" in calls[1]
    print(f"PASS: {calls}\n")

    print("=" * 60)
    print("TEST 4: after the cooldown window passes, the same kind alerts again")
    print("=" * 60)
    import time
    alerting._last_sent["kind_a"] = time.monotonic() - alerting.COOLDOWN_SECONDS - 1
    calls.clear()

    await alerting.send_alert("kind_a", "third kind_a, cooldown has passed")
    assert len(calls) == 1, f"FAIL: expected the cooldown to have lapsed, got {calls}"
    print("PASS: alert sent again once the cooldown window passed\n")


async def test_never_raises_on_http_failure():
    print("=" * 60)
    print("TEST 5: send_alert() never raises even if the HTTP call itself fails")
    print("=" * 60)
    settings.ALERT_TELEGRAM_TOKEN = "fake-token"
    settings.ALERT_TELEGRAM_CHAT_ID = "12345"
    alerting._last_sent.clear()

    class FailingAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            raise RuntimeError("simulated network failure")

    alerting.httpx.AsyncClient = FailingAsyncClient

    await alerting.send_alert("network_flaky", "this should not raise")
    print("PASS: no exception propagated\n")


async def run():
    await test_noop_when_unconfigured()
    await test_sends_with_expected_payload()
    await test_cooldown_suppresses_same_kind_but_not_different_kind()
    await test_never_raises_on_http_failure()
    print("ALL TESTS PASSED")


asyncio.run(run())
