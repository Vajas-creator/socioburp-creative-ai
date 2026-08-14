"""
Test for the internal test-number allowlist (see the Aug 2026 consolidated
fix list's "TEST NUMBER SETUP" section): app/allowlist.py grants a fixed
set of phone numbers unlimited access -- no credit deduction, no
quality-check regen-attempt cap -- while still running the FULL normal
flow (onboarding, quality gate, delivery, etc.) for them. This is an
explicit allowlist check at each spend/charge call site, not a general
bypass of the credit system.

Covers:
  - allowlist.has_unlimited_access() is a pure membership check: true for
    the listed test number, false for any other number.
  - app/router.py: the pre-generation credit gate ("You're out of
    credits!") is skipped for an allowlisted number even at 0 balance,
    but still fires normally for a non-allowlisted number at 0 balance
    (regression control -- this fix must not weaken the gate generally).
  - app/engine/orchestrator.py._run_generation(): an allowlisted number's
    successful generation does NOT call charge_for_generation (balance
    unchanged before/after), while a non-allowlisted number's generation
    still charges exactly 1 credit as before (regression control).
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_allowlist.db"
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

from PIL import Image  # noqa: E402
from app import allowlist  # noqa: E402
from app.whatsapp import client as wa_client  # noqa: E402

sent_texts = []


async def fake_send_text(to, body):
    sent_texts.append(body)


wa_client.send_text = fake_send_text


async def fake_send_buttons(to, body, buttons):
    sent_texts.append(body)


wa_client.send_buttons = fake_send_buttons

from app import router, payments  # noqa: E402
router.send_text = fake_send_text
payments.send_buttons = fake_send_buttons

from app.engine import router_intent  # noqa: E402


async def fake_router_classify(text):
    if not text or not text.strip():
        return {"intent": "OTHER", "command": None}
    return router_intent._fallback_classify(text)


router_intent.classify = fake_router_classify

generate_calls = []


async def fake_generate(business_id, msg):
    generate_calls.append(msg.type)


import app.engine.orchestrator as orch  # noqa: E402
orch.generate = fake_generate

from app.db import get_session  # noqa: E402
from app.models import Business, ConversationState, BrandProfile  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402
from app.credits import add_credits, get_balance  # noqa: E402
from app.engine.context import BusinessContext  # noqa: E402

TEST_NUMBER = "919818069317"
NORMAL_NUMBER = "919999999900"


def _make_business(phone, credits_amount=0, onboarding_state="done"):
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="salon", onboarding_state=onboarding_state)
        db.add(biz)
        db.flush()
        biz_id = biz.id
        if credits_amount:
            add_credits(db, biz_id, credits_amount, reason="signup_bonus")
        return biz_id


def png_bytes(color=(200, 50, 50), size=(64, 64)):
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def test_has_unlimited_access():
    print("=" * 60)
    print("TEST 1: has_unlimited_access() -- pure membership check")
    print("=" * 60)
    assert allowlist.has_unlimited_access(TEST_NUMBER) is True, "FAIL: expected the test number to be allowlisted"
    assert allowlist.has_unlimited_access(NORMAL_NUMBER) is False, "FAIL: expected a random number to NOT be allowlisted"
    assert allowlist.has_unlimited_access("") is False
    print("PASS\n")


async def test_router_credit_gate(allowlisted_biz_id):
    print("=" * 60)
    print("TEST 2: router credit gate is skipped for the allowlisted number at 0 balance")
    print("=" * 60)
    sent_texts.clear()
    generate_calls.clear()

    await router._process_message(allowlisted_biz_id, IncomingMessage(sender=TEST_NUMBER, type="text", text="make me a poster"))

    assert generate_calls == ["text"], f"FAIL: expected generate() to run despite 0 balance, got calls={generate_calls}, sent={sent_texts}"
    assert not any("out of credits" in s.lower() for s in sent_texts), f"FAIL: allowlisted number should never see the topup gate, got {sent_texts}"
    print("PASS: allowlisted number reached generate() at 0 balance, no topup prompt\n")

    print("=" * 60)
    print("TEST 3: router credit gate still fires normally for a non-allowlisted number at 0 balance")
    print("=" * 60)
    biz_id2 = _make_business(NORMAL_NUMBER, credits_amount=0)
    sent_texts.clear()
    generate_calls.clear()

    await router._process_message(biz_id2, IncomingMessage(sender=NORMAL_NUMBER, type="text", text="make me a poster"))

    assert generate_calls == [], f"FAIL: expected generate() NOT to run at 0 balance for a normal number, got {generate_calls}"
    assert any("out of credits" in s.lower() for s in sent_texts), f"FAIL: expected the topup gate to fire, got {sent_texts}"
    print("PASS: normal number still blocked by the credit gate at 0 balance (regression control)\n")


async def test_orchestrator_charge_skipped(allowlisted_biz_id):
    print("=" * 60)
    print("TEST 4: _run_generation() does not charge the allowlisted number")
    print("=" * 60)

    async def fake_content_policy_check(text):
        return {"allowed": True, "reason": None}

    orch.content_policy.check = fake_content_policy_check

    async def fake_build(ctx, brief):
        return {"image_prompt": f"prompt: {brief}", "headline_text": "Sale", "notes_for_caption": brief}

    orch.prompt_builder.build = fake_build

    async def fake_generate_images(prompt, count=2, reference_image=None):
        return [png_bytes()] * count

    orch.image_gen.generate_images = fake_generate_images

    async def fake_score_and_pick(images):
        return {"best_index": 0, "best_score": 90, "issues": []}

    orch.quality.score_and_pick = fake_score_and_pick

    async def fake_caption_generate(ctx, notes):
        return {"caption": "Nice!", "hashtags": "#offer"}

    orch.caption_engine.generate = fake_caption_generate

    def fake_upload_creative(business_id, generation_id, image_bytes):
        return f"https://fake.example.com/creatives/{generation_id}.png"

    def fake_upload_base_image(business_id, generation_id, image_bytes):
        return f"https://fake.example.com/creatives/{generation_id}_base.png"

    orch.upload_creative = fake_upload_creative
    orch.upload_base_image = fake_upload_base_image

    async def fake_deliver_creative(phone, business_id, generation_id, image_url, caption):
        pass

    orch._deliver_creative = fake_deliver_creative

    import uuid as uuid_module

    # Reuses the same allowlisted business created for TEST 2/3 (phone is
    # unique per Business row, and this IS the number under test) -- still
    # at 0 credits, since the router-level test above must not have
    # charged it either. Balance must stay at 0 before and after this
    # successful generation too.
    biz_id = allowlisted_biz_id
    with get_session() as db:
        db.add(BrandProfile(business_id=biz_id))
        db.add(ConversationState(business_id=biz_id))
    ctx = BusinessContext(name="Test Biz", industry="salon")

    balance_before = get_balance(biz_id)
    await orch._run_generation(
        biz_id, TEST_NUMBER, ctx, "create a weekend offer post", "create a weekend offer post",
        last_generation_id=uuid_module.uuid4(), is_revision=False, trigger_source="specific_enough",
    )
    balance_after = get_balance(biz_id)
    assert balance_before == 0 and balance_after == 0, f"FAIL: expected balance to stay 0, got before={balance_before} after={balance_after}"
    print(f"PASS: allowlisted number balance unchanged ({balance_before} -> {balance_after}), no charge\n")

    print("=" * 60)
    print("TEST 5: _run_generation() still charges a normal number (regression control)")
    print("=" * 60)
    biz_id2 = _make_business("919999999901", credits_amount=20)
    with get_session() as db:
        db.add(BrandProfile(business_id=biz_id2))
        db.add(ConversationState(business_id=biz_id2))
    ctx2 = BusinessContext(name="Test Biz 2", industry="salon")

    balance_before2 = get_balance(biz_id2)
    await orch._run_generation(
        biz_id2, NORMAL_NUMBER, ctx2, "create a weekend offer post", "create a weekend offer post",
        last_generation_id=uuid_module.uuid4(), is_revision=False, trigger_source="specific_enough",
    )
    balance_after2 = get_balance(biz_id2)
    assert balance_after2 == balance_before2 - 1, f"FAIL: expected a 1-credit charge, got before={balance_before2} after={balance_after2}"
    print(f"PASS: normal number charged as expected ({balance_before2} -> {balance_after2})\n")


async def run():
    test_has_unlimited_access()
    allowlisted_biz_id = _make_business(TEST_NUMBER, credits_amount=0)
    await test_router_credit_gate(allowlisted_biz_id)
    await test_orchestrator_charge_skipped(allowlisted_biz_id)
    print("ALL TESTS PASSED")


asyncio.run(run())
