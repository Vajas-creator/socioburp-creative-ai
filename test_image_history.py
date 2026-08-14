"""
Test for conversational image memory (Priority 3 of the Aug 2026
consolidated fix list): "change that background" or "use the second one"
should resolve against real conversation context (uploaded photos AND
generated creatives, in order) instead of forcing a re-upload or blindly
assuming "the last thing." See app/engine/image_history.py and
ConversationState.recent_images.

Covers:
  - record_image()/get_history(): appends in order, caps at MAX_HISTORY,
    survives across separate calls (persisted on ConversationState).
  - resolve_reference() is a no-op (None) with 0 or 1 images in history --
    the existing "most recent" default should handle those cases exactly
    as before this feature existed.
  - resolve_reference() with 2+ images picks the one the (mocked)
    classifier points at, or None if it says the reference is ambiguous/
    not about a past image.
  - orchestrator._run_generation()'s REVISE path: with 2+ images in
    history and a resolvable reference, the OLDER image is used as the
    edit base (not just last_generation_id) -- proving disambiguation
    actually changes behavior, not just that the function exists.
  - image_intent.start() records an uploaded photo immediately, before
    any generation happens from it.
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_image_history.db"
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


from app.engine import image_history  # noqa: E402
from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile  # noqa: E402
from app.credits import add_credits  # noqa: E402


def _make_business(phone):
    with get_session() as db:
        biz = Business(phone=phone, name="Resin Decor Co", industry="home decor", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        db.add(BrandProfile(business_id=biz_id, tone="elegant"))
        add_credits(db, biz_id, 20, reason="signup_bonus")
        return biz_id


def test_record_and_get_history():
    print("=" * 60)
    print("TEST 1: record_image()/get_history() -- order preserved, capped at MAX_HISTORY")
    print("=" * 60)
    biz_id = _make_business("919999999801")

    for i in range(image_history.MAX_HISTORY + 3):
        image_history.record_image(biz_id, "generated", f"https://fake.example.com/img{i}.png", f"image number {i}")

    history = image_history.get_history(biz_id)
    assert len(history) == image_history.MAX_HISTORY, f"FAIL: expected capped at {image_history.MAX_HISTORY}, got {len(history)}"
    assert history[-1]["url"] == f"https://fake.example.com/img{image_history.MAX_HISTORY + 2}.png", (
        f"FAIL: expected the newest record last, got {history[-1]}"
    )
    print(f"PASS: {len(history)} entries kept, newest last\n")


async def test_resolve_reference_no_op_cases():
    print("=" * 60)
    print("TEST 2: resolve_reference() is a no-op with 0 or 1 images")
    print("=" * 60)
    biz_id = _make_business("919999999802")

    result = await image_history.resolve_reference(biz_id, "use the second one")
    assert result is None, f"FAIL: expected None with 0 images, got {result}"

    image_history.record_image(biz_id, "generated", "https://fake.example.com/only.png", "the only image")
    result = await image_history.resolve_reference(biz_id, "change that background")
    assert result is None, f"FAIL: expected None with only 1 image (existing default should handle it), got {result}"
    print("PASS: no-op with 0 or 1 images in history\n")


async def test_resolve_reference_disambiguates():
    print("=" * 60)
    print("TEST 3: resolve_reference() picks the right image with 2+ in history")
    print("=" * 60)
    biz_id = _make_business("919999999803")

    image_history.record_image(biz_id, "uploaded", "https://fake.example.com/first.png", "product photo of a resin coaster")
    image_history.record_image(biz_id, "generated", "https://fake.example.com/second.png", "Diwali sale post")

    from app.anthropic_client import create_message as real_create_message

    class FakeContent:
        def __init__(self, text):
            self.text = text

    class FakeResponse:
        def __init__(self, text):
            self.content = [FakeContent(text)]

    async def fake_create_message(**kwargs):
        return FakeResponse('{"index": 1}')

    import app.engine.image_history as ih_module
    ih_module.create_message = fake_create_message

    result = await image_history.resolve_reference(biz_id, "use the second one I sent")
    assert result is not None and result["url"] == "https://fake.example.com/first.png", (
        f"FAIL: expected the first (index 1) image resolved, got {result}"
    )
    print(f"PASS: resolved to {result}\n")

    print("=" * 60)
    print("TEST 4: resolve_reference() returns None when the classifier says it's ambiguous")
    print("=" * 60)

    async def fake_create_message_null(**kwargs):
        return FakeResponse('{"index": null}')

    ih_module.create_message = fake_create_message_null
    result = await image_history.resolve_reference(biz_id, "make it more premium")
    assert result is None, f"FAIL: expected None, got {result}"
    print("PASS: correctly returned None for an ambiguous/non-referencing message\n")

    ih_module.create_message = real_create_message


async def test_revision_uses_resolved_reference():
    print("=" * 60)
    print("TEST 5: orchestrator._run_generation() REVISE path uses a resolved (non-last) image as the edit base")
    print("=" * 60)

    from app.whatsapp import client as wa_client

    async def fake_send_text(to, body):
        pass

    async def fake_send_image(to, image_url, caption=""):
        pass

    async def fake_send_image_with_button(to, image_url, body, button_id, button_label):
        pass

    wa_client.send_text = fake_send_text
    wa_client.send_image = fake_send_image
    wa_client.send_image_with_button = fake_send_image_with_button

    from app.engine import orchestrator as orch
    orch.send_text = fake_send_text
    orch.send_image = fake_send_image
    orch.send_image_with_button = fake_send_image_with_button

    from app.engine import prompt_builder
    prompt_builder_calls = []

    async def fake_build(ctx, user_brief):
        prompt_builder_calls.append(user_brief)
        return {"image_prompt": f"prompt: {user_brief}", "headline_text": "Sale", "notes_for_caption": user_brief}

    prompt_builder.build = fake_build
    orch.prompt_builder.build = fake_build

    from app.engine import image_gen
    image_gen_calls = []

    async def fake_generate_images(prompt, count=2, reference_image=None):
        image_gen_calls.append(reference_image)
        return [png_bytes()] * count

    image_gen.generate_images = fake_generate_images
    orch.image_gen.generate_images = fake_generate_images

    from app.engine import quality

    async def fake_score_and_pick(images):
        return {"best_index": 0, "best_score": 90, "issues": []}

    quality.score_and_pick = fake_score_and_pick
    orch.quality.score_and_pick = fake_score_and_pick

    from app.engine import caption as caption_engine

    async def fake_caption_generate(ctx, notes_for_caption):
        return {"caption": "Nice!", "hashtags": "#offer"}

    caption_engine.generate = fake_caption_generate
    orch.caption_engine.generate = fake_caption_generate

    import httpx as httpx_module

    class FakeHttpResponse:
        status_code = 200
        content = b"FAKE-OLDER-IMAGE-BYTES"

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return FakeHttpResponse()

    orch.httpx.AsyncClient = FakeAsyncClient

    def fake_upload_creative(business_id, generation_id, image_bytes):
        return f"https://fake.example.com/creatives/{generation_id}.png"

    def fake_upload_base_image(business_id, generation_id, image_bytes):
        return f"https://fake.example.com/creatives/{generation_id}_base.png"

    orch.upload_creative = fake_upload_creative
    orch.upload_base_image = fake_upload_base_image

    from app.engine import image_history as ih_module

    async def fake_resolve_reference(business_id, text):
        # Simulates a genuinely older image (NOT the most recent
        # generation) being resolved from the client's message.
        return {"kind": "uploaded", "url": "https://fake.example.com/OLDER.png", "label": "an older uploaded photo"}

    ih_module.resolve_reference = fake_resolve_reference
    orch.image_history.resolve_reference = fake_resolve_reference

    from app.db import get_session
    from app.models import Business, BrandProfile
    from app.engine.context import BusinessContext
    from app.credits import add_credits

    import uuid as uuid_module
    from app.models import ConversationState

    phone = "919999999804"
    with get_session() as db:
        biz = Business(phone=phone, name="Resin Decor Co", industry="home decor", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        db.add(BrandProfile(business_id=biz_id, tone="elegant"))
        # A ConversationState row, same as generate() always ensures exists
        # before ever reaching _run_generation in the real flow -- calling
        # _run_generation directly in this test bypasses that, so it's
        # created explicitly here.
        db.add(ConversationState(business_id=biz_id))
        add_credits(db, biz_id, 20, reason="signup_bonus")

    ctx = BusinessContext(name="Resin Decor Co", industry="home decor", tone="elegant")

    # A fake (non-None) last_generation_id -- this is a REVISE, so a real
    # prior generation would exist; using a placeholder here skips
    # _run_generation's real "very first generation" branch (which makes
    # its own separate, unmocked Claude call this test isn't set up for),
    # without affecting what's under test: the resolved-reference lookup
    # short-circuits the parent-generation DB lookup entirely regardless
    # of this value, since fake_resolve_reference above always returns a
    # match.
    await orch._run_generation(
        biz_id, phone, ctx, "use the second one I sent, change the background to black", "use the second one I sent, change the background to black",
        last_generation_id=uuid_module.uuid4(), is_revision=True, trigger_source="revision", reference_image=None,
    )

    assert any("older uploaded photo" in call for call in prompt_builder_calls), (
        f"FAIL: expected the resolved (older) image's label to inform the prompt, got {prompt_builder_calls}"
    )
    assert any(call == b"FAKE-OLDER-IMAGE-BYTES" for call in image_gen_calls), (
        f"FAIL: expected the RESOLVED older image's bytes to be used as the edit reference, not last_generation_id's, got {image_gen_calls}"
    )
    print("PASS: revision correctly used the resolved (non-last) image as its edit base\n")


async def test_image_intent_records_upload_immediately():
    print("=" * 60)
    print("TEST 6: image_intent.start() records the uploaded photo before any generation")
    print("=" * 60)

    from app.whatsapp import client as wa_client

    async def fake_send_text(to, body):
        pass

    async def fake_send_buttons(to, body, buttons):
        pass

    async def fake_download_media(media_id):
        return png_bytes()

    wa_client.send_text = fake_send_text
    wa_client.send_buttons = fake_send_buttons
    wa_client.download_media = fake_download_media

    from app.engine import image_intent
    image_intent.send_text = fake_send_text
    image_intent.send_buttons = fake_send_buttons
    image_intent.download_media = fake_download_media

    def fake_upload_reference_image(business_id, image_bytes):
        return "https://fake.example.com/references/uploaded.png"

    image_intent.upload_reference_image = fake_upload_reference_image

    from app.db import get_session
    from app.models import Business
    from app.schemas import IncomingMessage
    from app.credits import add_credits

    phone = "919999999805"
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="salon", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        add_credits(db, biz_id, 20, reason="signup_bonus")

    await image_intent.start(biz_id, IncomingMessage(sender=phone, type="image", media_id="wamid123", text=None))

    history = image_history.get_history(biz_id)
    assert len(history) == 1, f"FAIL: expected 1 recorded image, got {history}"
    assert history[0]["kind"] == "uploaded", f"FAIL: expected kind='uploaded', got {history[0]}"
    assert history[0]["url"] == "https://fake.example.com/references/uploaded.png"
    print(f"PASS: uploaded photo recorded immediately: {history[0]}\n")


async def run():
    test_record_and_get_history()
    await test_resolve_reference_no_op_cases()
    await test_resolve_reference_disambiguates()
    await test_revision_uses_resolved_reference()
    await test_image_intent_records_upload_immediately()
    print("ALL TESTS PASSED")


asyncio.run(run())
