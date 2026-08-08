"""
Test for the retry-on-APIConnectionError wrapper in app/anthropic_client.py
(create_message()), added alongside the custom httpx transport config and
richer failure logging (Aug 8, 2026 production connection-error fixes).

Covers:
  - Retries exactly 3 attempts total on APIConnectionError, then re-raises
  - Succeeds without exhausting retries if a later attempt works
  - Does NOT retry non-APIConnectionError exceptions -- fails immediately
  - Backoff sleeps use the documented 1s/2s schedule between attempts
"""
import sys
import os

sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_anthropic_retry.db")
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

import asyncio  # noqa: E402

import httpx  # noqa: E402
from anthropic import APIConnectionError, AuthenticationError  # noqa: E402

from app import anthropic_client  # noqa: E402

_FAKE_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _make_auth_error():
    return AuthenticationError(
        message="invalid x-api-key",
        response=httpx.Response(401, request=_FAKE_REQUEST, json={"error": {"message": "bad key"}}),
        body={"error": {"message": "bad key"}},
    )


async def run():
    print("=" * 60)
    print("TEST 1: exhausts all 3 attempts on persistent APIConnectionError, then raises")
    print("=" * 60)
    calls = {"n": 0}

    async def always_fails(**kwargs):
        calls["n"] += 1
        raise APIConnectionError(request=_FAKE_REQUEST)

    anthropic_client.client.messages.create = always_fails
    sleeps = []
    real_sleep = asyncio.sleep
    asyncio.sleep = lambda s: (sleeps.append(s), real_sleep(0))[1]
    try:
        try:
            await anthropic_client.create_message(model="x", max_tokens=5, messages=[])
            assert False, "FAIL: expected APIConnectionError to propagate after exhausting retries"
        except APIConnectionError:
            pass
    finally:
        asyncio.sleep = real_sleep
    assert calls["n"] == 3, f"FAIL: expected exactly 3 attempts, got {calls['n']}"
    assert sleeps == [1.0, 2.0], f"FAIL: expected backoff [1.0, 2.0] between 3 attempts, got {sleeps}"
    print(f"PASS: retried {calls['n']} times total with backoff {sleeps}, then re-raised\n")

    print("=" * 60)
    print("TEST 2: succeeds on a later attempt without exhausting all 3")
    print("=" * 60)
    calls = {"n": 0}

    async def fails_then_succeeds(**kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            raise APIConnectionError(request=_FAKE_REQUEST)
        return "OK"

    anthropic_client.client.messages.create = fails_then_succeeds
    real_sleep = asyncio.sleep
    asyncio.sleep = lambda s: real_sleep(0)
    try:
        result = await anthropic_client.create_message(model="x", max_tokens=5, messages=[])
    finally:
        asyncio.sleep = real_sleep
    assert result == "OK", f"FAIL: expected successful result once the transient failure clears, got {result!r}"
    assert calls["n"] == 2, f"FAIL: expected exactly 2 attempts (1 failure + 1 success), got {calls['n']}"
    print(f"PASS: recovered after {calls['n']} attempts, no unnecessary retries\n")

    print("=" * 60)
    print("TEST 3: non-APIConnectionError is NOT retried -- fails on first attempt")
    print("=" * 60)
    calls = {"n": 0}

    async def auth_error(**kwargs):
        calls["n"] += 1
        raise _make_auth_error()

    anthropic_client.client.messages.create = auth_error
    try:
        await anthropic_client.create_message(model="x", max_tokens=5, messages=[])
        assert False, "FAIL: expected AuthenticationError to propagate"
    except AuthenticationError:
        pass
    assert calls["n"] == 1, f"FAIL: expected exactly 1 attempt (no retry on non-connection errors), got {calls['n']}"
    print(f"PASS: non-APIConnectionError failed immediately after {calls['n']} attempt, no retry\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
