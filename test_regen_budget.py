"""
Smoke test for the regen budget mechanism (app/credits.py: regen_within_budget,
record_regen_used, and the allowance earned in add_credits).

Quality is mocked to always score below REGEN_THRESHOLD, so every generation
wants a regen. With a 6-credit signup bonus, allowance = 6 // 3 = 2 regens.

  Gen 1: allowance available (0 used < 2) -> regen runs, delivered, charged
  Gen 2: allowance available (1 used < 2) -> regen runs, delivered, charged
  Gen 3: allowance exhausted (2 used, 2 allowance) -> BLOCKED: no regen, no
         delivery, no charge, a 'blocked' Generation row saved instead
  Topup +3 credits -> allowance += 3 // 3 = 1 (now 3 total, 2 used)
  Gen 4: allowance available again (2 used < 3) -> regen runs, delivered, charged
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_budget.db"
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


def png_bytes(color=(200, 50, 50), size=(64, 64)):
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


# --- Mock WhatsApp sends ---
from app.whatsapp import client as wa_client  # noqa: E402

sent_texts, sent_images = [], []


async def fake_send_text(to, body):
    sent_texts.append(body)
    print(f"[SEND TEXT]: {body}\n")


async def fake_send_image(to, image_url, caption=""):
    sent_images.append(image_url)
    print(f"[SEND IMAGE]: {image_url}\n")


wa_client.send_text = fake_send_text
wa_client.send_image = fake_send_image

from app.engine import orchestrator  # noqa: E402
orchestrator.send_text = fake_send_text
orchestrator.send_image = fake_send_image

# --- Mock intent + concept proposal: always specific, straight to generation ---
from app.engine import intent as intent_engine  # noqa: E402


async def fake_classify(user_message):
    return {"intent": "GENERATE", "brief": user_message}


intent_engine.classify = fake_classify
orchestrator.intent_engine.classify = fake_classify

from app.engine import concept_proposal  # noqa: E402


async def fake_decide(ctx, user_message):
    return {"decision": "SPECIFIC_ENOUGH", "brief": user_message}


concept_proposal.decide = fake_decide
orchestrator.concept_proposal.decide = fake_decide

# --- Mock the pipeline internals ---
from app.engine import prompt_builder  # noqa: E402


async def fake_build(ctx, user_brief):
    return {"image_prompt": f"creative: {user_brief}", "notes_for_caption": user_brief}


prompt_builder.build = fake_build
orchestrator.prompt_builder.build = fake_build

from app.engine import image_gen  # noqa: E402

image_gen_call_count = {"n": 0}


async def fake_generate_images(prompt, count=2, reference_image=None):
    image_gen_call_count["n"] += 1
    return [png_bytes(), png_bytes()]


image_gen.generate_images = fake_generate_images
orchestrator.image_gen.generate_images = fake_generate_images

from app.engine import quality  # noqa: E402


async def fake_score_and_pick_low(images):
    # Always below REGEN_THRESHOLD (60) -- every generation wants a regen.
    return {"best_index": 0, "best_score": 30, "issues": ["too generic"]}


quality.score_and_pick = fake_score_and_pick_low
orchestrator.quality.score_and_pick = fake_score_and_pick_low

from app.engine import caption as caption_engine  # noqa: E402


async def fake_caption_generate(ctx, notes_for_caption):
    return {"caption": f"Check this out! {notes_for_caption}", "hashtags": "#offer #socioburp"}


caption_engine.generate = fake_caption_generate
orchestrator.caption_engine.generate = fake_caption_generate

uploaded = {}


def fake_upload_creative(business_id, generation_id, image_bytes):
    url = f"https://fake-cdn.example.com/{business_id}/{generation_id}.png"
    uploaded[url] = image_bytes
    return url


def fake_upload_base_image(business_id, generation_id, image_bytes):
    url = f"https://fake-cdn.example.com/{business_id}/{generation_id}_base.png"
    uploaded[url] = image_bytes
    return url


orchestrator.upload_creative = fake_upload_creative
orchestrator.upload_base_image = fake_upload_base_image

# No logo on the test business, so the httpx logo-fetch path is never hit —
# no need to mock httpx here (unlike test_revision_classifier.py).

from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile, Generation  # noqa: E402
from app.credits import add_credits, get_balance  # noqa: E402
from app.engine.orchestrator import generate  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402


async def run():
    phone = "919999999997"
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="restaurant", onboarding_state="done")
        db.add(biz)
        db.flush()
        business_id = biz.id
        db.add(BrandProfile(business_id=business_id, tone="premium"))
        add_credits(db, business_id, 6, reason="signup_bonus")  # -> allowance = 6 // 3 = 2

    def counts():
        with get_session() as db:
            b = db.query(Business).filter(Business.id == business_id).first()
            return b.regen_allowance_this_cycle, b.regens_used_this_cycle

    allowance, used = counts()
    assert allowance == 2, f"FAIL: expected allowance=2 from a 6-credit signup bonus, got {allowance}"
    assert used == 0, f"FAIL: expected 0 regens used at start, got {used}"
    print(f"SETUP: signup bonus 6 credits -> regen allowance={allowance}, used={used}, balance={get_balance(business_id)}\n")

    for i in (1, 2):
        print("=" * 60)
        print(f"TEST: generation {i} -- allowance available, regen should run and deliver normally")
        print("=" * 60)
        sent_texts.clear()
        sent_images.clear()
        balance_before = get_balance(business_id)
        await generate(business_id, IncomingMessage(sender=phone, type="text", text=f"Create offer post {i}"))
        allowance, used = counts()
        balance_after = get_balance(business_id)
        assert used == i, f"FAIL: expected regens_used={i} after generation {i}, got {used}"
        assert len(sent_images) == 1, f"FAIL: expected the creative to be delivered, got {sent_images}"
        assert balance_after == balance_before - 1, f"FAIL: expected 1 credit charged, balance {balance_before} -> {balance_after}"
        print(f"PASS: gen {i} delivered normally, 1 credit charged (balance {balance_before} -> {balance_after}), regens_used={used}\n")

    print("=" * 60)
    print("TEST: generation 3 -- allowance exhausted, must BLOCK (no charge, no delivery)")
    print("=" * 60)
    sent_texts.clear()
    sent_images.clear()
    balance_before = get_balance(business_id)
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="Create offer post 3"))
    allowance, used = counts()
    balance_after = get_balance(business_id)
    with get_session() as db:
        blocked_rows = db.query(Generation).filter(
            Generation.business_id == business_id, Generation.status == "blocked"
        ).count()
    assert used == 2, f"FAIL: regens_used should stay at 2 (allowance exhausted, no new regen performed), got {used}"
    assert len(sent_images) == 0, f"FAIL: nothing should have been delivered when blocked, got {sent_images}"
    assert balance_after == balance_before, f"FAIL: no credit should be charged when blocked, balance {balance_before} -> {balance_after}"
    assert len(sent_texts) == 2, f"FAIL: expected 2 messages ('Creating your design...' + the block notice), got {sent_texts}"
    assert "quality bar" in sent_texts[-1], f"FAIL: expected the block message last, got: {sent_texts[-1]!r}"
    assert blocked_rows == 1, f"FAIL: expected 1 Generation row with status='blocked', found {blocked_rows}"
    print(f"PASS: blocked correctly -- no charge, no delivery, 1 'blocked' row saved, balance unchanged at {balance_after}\n")

    print("=" * 60)
    print("TEST: topup +3 credits -> allowance grows, regen works again")
    print("=" * 60)
    with get_session() as db:
        add_credits(db, business_id, 3, reason="topup")  # -> allowance += 3 // 3 = 1
    allowance, used = counts()
    assert allowance == 3, f"FAIL: expected allowance=3 after a +3 topup (2 + 1 earned), got {allowance}"
    assert used == 2, f"FAIL: topup should not reset regens_used, expected still 2, got {used}"
    print(f"After topup: allowance={allowance}, used={used} (unchanged, correctly additive not reset)\n")

    sent_texts.clear()
    sent_images.clear()
    balance_before = get_balance(business_id)
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="Create offer post 4"))
    allowance, used = counts()
    balance_after = get_balance(business_id)
    assert used == 3, f"FAIL: expected regens_used=3 after the topup restored allowance, got {used}"
    assert len(sent_images) == 1, f"FAIL: expected the creative to be delivered after topup restored allowance, got {sent_images}"
    assert balance_after == balance_before - 1, f"FAIL: expected 1 credit charged, balance {balance_before} -> {balance_after}"
    print(f"PASS: topup restored allowance -- gen 4 delivered and charged normally (balance {balance_before} -> {balance_after})\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
