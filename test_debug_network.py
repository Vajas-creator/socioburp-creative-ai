"""
Test for the shared-secret gate on GET /debug/network-check
(app/debug_network.py + app/config.py's DEBUG_NETWORK_SECRET).

Uses FastAPI's TestClient against the real endpoint, with the four
underlying network-probe functions mocked out (no real DNS/TCP/HTTPS/SDK
calls needed to prove the gate itself works). Covers:
  - DEBUG_NETWORK_SECRET unset -> 403 always, even with a "correct"-looking
    query param (fails closed on misconfiguration, never fails open)
  - DEBUG_NETWORK_SECRET set, no ?secret= given -> 403
  - DEBUG_NETWORK_SECRET set, wrong ?secret= -> 403
  - DEBUG_NETWORK_SECRET set, correct ?secret= -> 200, diagnostic body returned
"""
import sys
import os

sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_debug_network.db")
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

from fastapi.testclient import TestClient  # noqa: E402
from app import debug_network  # noqa: E402
from app.config import settings  # noqa: E402

# Mock every real network call out — this test is about the auth gate, not
# the diagnostics themselves (those were smoke-tested live separately).
debug_network._dns_test = lambda host: {"ok": True, "mocked": True}


async def _fake_async_probe(*args, **kwargs):
    return {"ok": True, "mocked": True}


debug_network._tcp_connect_test = _fake_async_probe
debug_network._https_get_test = _fake_async_probe
debug_network._anthropic_sdk_test = _fake_async_probe

from fastapi import FastAPI  # noqa: E402
app = FastAPI()
app.include_router(debug_network.router)
client = TestClient(app)


def run():
    print("=" * 60)
    print("TEST 1: DEBUG_NETWORK_SECRET unset -> 403 always, fails closed")
    print("=" * 60)
    settings.DEBUG_NETWORK_SECRET = ""
    resp = client.get("/debug/network-check", params={"secret": "anything-at-all"})
    assert resp.status_code == 403, f"FAIL: expected 403 with no secret configured, got {resp.status_code}"
    print("PASS: unset secret correctly locks the endpoint regardless of query param\n")

    print("=" * 60)
    print("TEST 2: secret configured, no ?secret= given -> 403")
    print("=" * 60)
    settings.DEBUG_NETWORK_SECRET = "correct-horse-battery-staple"
    resp = client.get("/debug/network-check")
    assert resp.status_code == 403, f"FAIL: expected 403 with missing secret, got {resp.status_code}"
    print("PASS: missing ?secret= correctly rejected\n")

    print("=" * 60)
    print("TEST 3: secret configured, wrong ?secret= -> 403")
    print("=" * 60)
    resp = client.get("/debug/network-check", params={"secret": "wrong-guess"})
    assert resp.status_code == 403, f"FAIL: expected 403 with wrong secret, got {resp.status_code}"
    print("PASS: wrong ?secret= correctly rejected\n")

    print("=" * 60)
    print("TEST 4: secret configured, correct ?secret= -> 200, diagnostic body returned")
    print("=" * 60)
    resp = client.get("/debug/network-check", params={"secret": "correct-horse-battery-staple"})
    assert resp.status_code == 200, f"FAIL: expected 200 with correct secret, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert "4_full_anthropic_sdk_call" in data, f"FAIL: expected diagnostic body, got {data}"
    print(f"PASS: correct ?secret= let the request through, got diagnostic body: {list(data.keys())}\n")

    print("ALL TESTS PASSED")


run()
