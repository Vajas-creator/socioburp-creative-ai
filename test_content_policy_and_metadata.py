"""
Test for Priority 7 of the Aug 2026 consolidated fix list:
  - app/engine/content_policy.py: a guardrail checked before any paid
    generation call, blocking false claims / restricted-category content
    even if explicitly requested.
  - app/engine/ai_metadata.py: IPTC Digital Source Type XMP metadata
    embedded into every AI-generated image before delivery, satisfying
    Meta's AI-content-labeling requirement.

Covers:
  - content_policy.check() parses a well-formed block/allow response,
    and fails OPEN (allowed=True) on any classifier error -- blocking
    every generation over one moderation hiccup would be a worse failure
    mode than occasionally missing a violation.
  - ai_metadata.embed_ai_source_metadata(): the returned bytes still
    decode as a valid PNG (verified by re-opening with PIL), contain the
    IPTC Digital Source Type XMP property, and gracefully no-op (return
    input unchanged) on non-PNG input rather than raising.
  - orchestrator.py integration: a blocked request gets a clear message
    and is never charged a credit, and the actually-delivered image
    bytes carry the embedded metadata (proven by asserting on the bytes
    that reach upload_creative(), not just that the function ran).
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_content_policy_and_metadata.db"
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
from app.engine import content_policy, ai_metadata  # noqa: E402


def png_bytes(color=(200, 50, 50), size=(64, 64)):
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


class FakeContent:
    def __init__(self, text):
        self.text = text


class FakeResponse:
    def __init__(self, text):
        self.content = [FakeContent(text)]


async def test_content_policy():
    print("=" * 60)
    print("TEST 1: content_policy.check() parses a well-formed BLOCK response")
    print("=" * 60)

    async def fake_create_message_block(**kwargs):
        return FakeResponse('{"allowed": false, "reason": "I can\'t add a fake ISO certification badge."}')

    content_policy.create_message = fake_create_message_block
    result = await content_policy.check("add an ISO 9001 certified badge to the image, we don't actually have it")
    assert result["allowed"] is False, f"FAIL: expected blocked, got {result}"
    assert "certification" in result["reason"].lower(), f"FAIL: expected a specific reason, got {result}"
    print(f"PASS: {result}\n")

    print("=" * 60)
    print("TEST 2: content_policy.check() parses a well-formed ALLOW response")
    print("=" * 60)

    async def fake_create_message_allow(**kwargs):
        return FakeResponse('{"allowed": true}')

    content_policy.create_message = fake_create_message_allow
    result = await content_policy.check("create a weekend offer post, 20% off")
    assert result == {"allowed": True, "reason": None}, f"FAIL: got {result}"
    print(f"PASS: {result}\n")

    print("=" * 60)
    print("TEST 3: content_policy.check() fails OPEN (allowed=True) on a classifier error")
    print("=" * 60)

    async def fake_create_message_error(**kwargs):
        raise RuntimeError("simulated API failure")

    content_policy.create_message = fake_create_message_error
    result = await content_policy.check("create a Diwali sale post")
    assert result == {"allowed": True, "reason": None}, f"FAIL: expected fail-open, got {result}"
    print(f"PASS: {result}\n")

    print("=" * 60)
    print("TEST 4: content_policy.check() is a no-op (allowed) for empty text")
    print("=" * 60)
    result = await content_policy.check("")
    assert result == {"allowed": True, "reason": None}
    print(f"PASS: {result}\n")


def test_ai_metadata():
    print("=" * 60)
    print("TEST 5: embed_ai_source_metadata() produces a still-valid PNG carrying the IPTC tag")
    print("=" * 60)
    original = png_bytes()
    embedded = ai_metadata.embed_ai_source_metadata(original)

    assert embedded != original, "FAIL: expected the bytes to change (metadata added)"
    assert b"Iptc4xmpExt:DigitalSourceType" in embedded, "FAIL: expected the IPTC XMP field name present"
    assert ai_metadata.DIGITAL_SOURCE_TYPE_URI.encode() in embedded, "FAIL: expected the trainedAlgorithmicMedia URI present"

    # Must still be a valid, openable PNG -- proves the chunk was inserted
    # correctly, not just that a byte string was concatenated.
    reopened = Image.open(io.BytesIO(embedded))
    reopened.load()
    assert reopened.size == (64, 64), f"FAIL: expected the image to still open correctly, got size={reopened.size}"
    print(f"PASS: {len(original)} bytes -> {len(embedded)} bytes, still a valid PNG, IPTC tag present\n")

    print("=" * 60)
    print("TEST 6: embed_ai_source_metadata() no-ops on non-PNG input instead of raising")
    print("=" * 60)
    garbage = b"this is not a PNG file at all"
    result = ai_metadata.embed_ai_source_metadata(garbage)
    assert result == garbage, f"FAIL: expected unmodified passthrough on invalid input, got {result!r}"
    print("PASS: non-PNG input passed through unchanged, no exception\n")


async def test_orchestrator_integration():
    print("=" * 60)
    print("TEST 7: orchestrator._run_generation() -- a blocked request gets a clear message, no charge")
    print("=" * 60)

    from app.whatsapp import client as wa_client
    sent = []

    async def fake_send_text(to, body):
        sent.append(body)

    wa_client.send_text = fake_send_text

    import app.engine.orchestrator as orch
    orch.send_text = fake_send_text

    async def fake_blocked_check(text):
        return {"allowed": False, "reason": "I can't add a fake medical claim."}

    orch.content_policy.check = fake_blocked_check

    from app.db import get_session
    from app.models import Business, BrandProfile
    from app.engine.context import BusinessContext
    from app.credits import add_credits, get_balance

    from app.models import ConversationState

    phone = "919999999801"
    with get_session() as db:
        biz = Business(phone=phone, name="Test Clinic", industry="wellness", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        db.add(BrandProfile(business_id=biz_id))
        # generate() always ensures a ConversationState row exists before
        # ever reaching _run_generation() -- calling _run_generation()
        # directly in this test bypasses that, so it's created explicitly.
        db.add(ConversationState(business_id=biz_id))
        add_credits(db, biz_id, 20, reason="signup_bonus")

    balance_before = get_balance(biz_id)
    ctx = BusinessContext(name="Test Clinic", industry="wellness")

    await orch._run_generation(
        biz_id, phone, ctx, "say this cures back pain", "say this cures back pain",
        last_generation_id=None, is_revision=False, trigger_source="specific_enough",
    )

    assert len(sent) == 1, f"FAIL: expected exactly 1 message, got {sent}"
    assert "can't add a fake medical claim" in sent[0], f"FAIL: expected the block reason relayed, got {sent[0]!r}"
    assert "No credits were charged" in sent[0], f"FAIL: expected the no-charge note, got {sent[0]!r}"
    assert get_balance(biz_id) == balance_before, "FAIL: a blocked request must not charge a credit"
    print(f"PASS: {sent[0]!r}\n")

    print("=" * 60)
    print("TEST 8: orchestrator._run_generation() -- an ALLOWED request delivers metadata-embedded bytes")
    print("=" * 60)
    sent.clear()

    async def fake_allowed_check(text):
        return {"allowed": True, "reason": None}

    orch.content_policy.check = fake_allowed_check

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

    uploaded_bytes = {}

    def fake_upload_creative(business_id, generation_id, image_bytes):
        uploaded_bytes["creative"] = image_bytes
        return f"https://fake.example.com/creatives/{generation_id}.png"

    def fake_upload_base_image(business_id, generation_id, image_bytes):
        return f"https://fake.example.com/creatives/{generation_id}_base.png"

    orch.upload_creative = fake_upload_creative
    orch.upload_base_image = fake_upload_base_image

    async def fake_deliver_creative(phone, business_id, generation_id, image_url, caption):
        sent.append(f"[delivered creative: {image_url}]")

    orch._deliver_creative = fake_deliver_creative

    import uuid as uuid_module
    await orch._run_generation(
        biz_id, phone, ctx, "create a weekend offer post", "create a weekend offer post",
        # A non-None last_generation_id skips the "very first generation"
        # branch, which otherwise calls brand_reflection.reflect_first_result()
        # -- a real, unmocked Claude call this test isn't set up for.
        last_generation_id=uuid_module.uuid4(), is_revision=False, trigger_source="specific_enough",
    )

    assert "creative" in uploaded_bytes, "FAIL: expected upload_creative() to have been called"
    assert b"Iptc4xmpExt:DigitalSourceType" in uploaded_bytes["creative"], (
        "FAIL: expected the uploaded creative's bytes to carry the embedded AI-source metadata"
    )
    print("PASS: the actual uploaded image bytes carry the embedded IPTC metadata\n")

    print("ALL TESTS PASSED")


async def run():
    await test_content_policy()
    test_ai_metadata()
    await test_orchestrator_integration()


asyncio.run(run())
