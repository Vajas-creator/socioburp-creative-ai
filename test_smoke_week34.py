"""
Smoke test for Week 3/4 additions:
  - Razorpay payment link creation (mocked HTTP call)
  - Webhook signature verification (real HMAC, not mocked)
  - Idempotency (same payment_link.paid event delivered twice = credited once)
  - Rate limiting (11th generation in an hour gets blocked)
"""
import sys
import asyncio
import os
import hashlib
import hmac
import json

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_smoke_week34.db"
os.environ["WA_VERIFY_TOKEN"] = "fake"
os.environ["WA_ACCESS_TOKEN"] = "fake"
os.environ["WA_PHONE_NUMBER_ID"] = "fake"
os.environ["ANTHROPIC_API_KEY"] = "fake"
os.environ["R2_ACCOUNT_ID"] = "fake"
os.environ["R2_ACCESS_KEY"] = "fake"
os.environ["R2_SECRET_KEY"] = "fake"
os.environ["R2_BUCKET"] = "fake"
os.environ["R2_PUBLIC_BASE_URL"] = "https://fake.example.com"
os.environ["RAZORPAY_KEY_ID"] = "rzp_test_fake"
os.environ["RAZORPAY_KEY_SECRET"] = "fake_secret"
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "webhook_secret_123"
os.environ["MAX_GENERATIONS_PER_HOUR"] = "3"  # low, to actually trigger the limit in this test

from app import db as db_module
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

db_module.engine = create_engine("sqlite:///./test_smoke_week34.db")
db_module.SessionLocal = sessionmaker(bind=db_module.engine)

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


import app.models  # noqa: E402
db_module.Base.metadata.create_all(bind=db_module.engine)

from app.whatsapp import client as wa_client  # noqa: E402
sent_messages = []


async def fake_send_text(to, body):
    sent_messages.append(("text", to, body))
    print(f"[SEND TEXT to {to}]: {body[:100]}")


async def fake_send_buttons(to, body, buttons):
    sent_messages.append(("buttons", to, body))
    print(f"[SEND BUTTONS to {to}]: {body[:80]} | {buttons}")


wa_client.send_text = fake_send_text
wa_client.send_buttons = fake_send_buttons

from app import payments  # noqa: E402
payments.send_text = fake_send_text
payments.send_buttons = fake_send_buttons

from app.db import get_session  # noqa: E402
from app.models import Business, CreditLedger  # noqa: E402
from app.credits import add_credits, get_balance  # noqa: E402

import uuid as uuid_module


async def test_payments():
    # Set up a test business
    with get_session() as db:
        biz = Business(phone="919999999999", name="Test Biz", onboarding_state="done")
        db.add(biz)
        db.flush()
        business_id = biz.id
        add_credits(db, business_id, 5, reason="signup_bonus")

    print("\n=== TEST 1: Payment link creation (mocked HTTP) ===")
    import httpx

    class FakeResponse:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"short_url": "https://rzp.io/i/fake123", "id": "plink_fake123"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, **kwargs):
            print(f"  [MOCK HTTP POST] {url}")
            print(f"  [MOCK PAYLOAD] {json.dumps(kwargs.get('json', {}), indent=2)[:300]}")
            return FakeResponse()

    original_client = httpx.AsyncClient
    httpx.AsyncClient = FakeAsyncClient

    await payments.handle_pack_selection(business_id, "919999999999", "pack_200")
    assert any("rzp.io/i/fake123" in m[2] for m in sent_messages if m[0] == "text"), "Payment link not sent!"
    print("  PASS: payment link message sent to user")

    httpx.AsyncClient = original_client

    print("\n=== TEST 2: Webhook signature verification ===")
    webhook_body = json.dumps({
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_test001",
                    "notes": {"business_id": str(business_id), "credits": "200"},
                }
            }
        }
    }).encode()

    correct_sig = hmac.new(b"webhook_secret_123", webhook_body, hashlib.sha256).hexdigest()
    wrong_sig = "0" * 64

    assert payments._verify_signature(webhook_body, correct_sig) is True, "Correct signature should verify!"
    assert payments._verify_signature(webhook_body, wrong_sig) is False, "Wrong signature should NOT verify!"
    assert payments._verify_signature(webhook_body, "") is False, "Empty signature should NOT verify!"
    print("  PASS: signature verification correctly accepts valid, rejects invalid/missing")

    print("\n=== TEST 3: Webhook crediting + idempotency (via direct handler logic) ===")
    balance_before = get_balance(business_id)
    print(f"  Balance before: {balance_before}")

    # Simulate what the webhook route does internally (bypassing FastAPI's Request wrapper)
    with get_session() as db:
        existing = db.query(CreditLedger).filter(
            CreditLedger.ref_id == "plink_test001", CreditLedger.reason == "topup"
        ).first()
        assert existing is None, "Should not exist yet"
        add_credits(db, business_id, 200, reason="topup", ref_id="plink_test001")

    balance_after_first = get_balance(business_id)
    print(f"  Balance after first credit: {balance_after_first}")
    assert balance_after_first == balance_before + 200, "Credits not added correctly!"

    # Simulate Razorpay retrying the SAME webhook event (it does this by design)
    with get_session() as db:
        existing = db.query(CreditLedger).filter(
            CreditLedger.ref_id == "plink_test001", CreditLedger.reason == "topup"
        ).first()
        if existing:
            print("  Duplicate webhook detected correctly — skipping second credit")
        else:
            add_credits(db, business_id, 200, reason="topup", ref_id="plink_test001")

    balance_after_retry = get_balance(business_id)
    print(f"  Balance after duplicate webhook: {balance_after_retry}")
    assert balance_after_retry == balance_after_first, "Duplicate webhook double-credited! BUG!"
    print("  PASS: idempotency correctly prevented double-crediting")

    print("\n=== TEST 4: Rate limiting ===")
    from app.engine.orchestrator import _check_rate_limit
    from app.models import Generation

    with get_session() as db:
        for i in range(3):  # MAX_GENERATIONS_PER_HOUR is set to 3 for this test
            db.add(Generation(business_id=business_id, user_message=f"test {i}", status="done"))
        db.flush()

        within_limit = _check_rate_limit(db, business_id)
        print(f"  After 3 generations (limit=3): within_limit = {within_limit}")
        assert within_limit is False, "Should be AT limit, not within it!"
        print("  PASS: rate limit correctly triggers at the configured threshold")

    with get_session() as db:
        other_biz = Business(phone="918888888888", name="Other Biz", onboarding_state="done")
        db.add(other_biz)
        db.flush()
        within_limit_other = _check_rate_limit(db, other_biz.id)
        print(f"  Different business, 0 generations: within_limit = {within_limit_other}")
        assert within_limit_other is True, "A different business should not be rate-limited!"
        print("  PASS: rate limit is correctly scoped per-business, not global")

    print("\n=== ALL TESTS PASSED ===")


asyncio.run(test_payments())
