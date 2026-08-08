"""
Test for the "client's specific instructions get lost" bug in
app/engine/orchestrator.py.

Previously, both the SPECIFIC_ENOUGH (single-message GENERATE, no proposal
needed) and REVISE (non-logo-position) paths built the actual image prompt
from a re-summarized "brief" (intent.classify()'s or
concept_proposal.decide()'s or revision_classifier.classify()'s one-line
paraphrase) instead of the client's own raw words -- exactly the kind of
lossy step that drops a detail like "change the background to black" or
"add a 25% off overlay" while keeping the other. The raw message was
threaded all the way through _run_generation() as `user_message`, but only
ever used for the Generation.user_message DB column, never for building
the prompt itself.

This test proves the fix: prompt_builder.build() now receives the client's
verbatim message on those two paths, not a re-derived summary -- by making
the mocked classify()/decide() functions deliberately return a DIFFERENT,
shorter "brief" than the raw message, and asserting prompt_builder saw the
raw text, not the shortened stand-in.

Also covers: a photo attached to the message (reference_image) is threaded
through to image_gen.generate_images() on both paths, so an uploaded
product photo is no longer silently ignored by the pipeline.
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_gen_instructions.db"
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


from app.whatsapp import client as wa_client  # noqa: E402

sent_texts, sent_images = [], []


async def fake_send_text(to, body):
    sent_texts.append(body)


async def fake_send_image(to, image_url, caption=""):
    sent_images.append(image_url)


async def fake_send_image_with_button(to, image_url, body, button_id, button_label):
    sent_images.append(image_url)


async def fake_download_media(media_id):
    return b"FAKE-PRODUCT-PHOTO-BYTES"


wa_client.send_text = fake_send_text
wa_client.send_image = fake_send_image
wa_client.send_image_with_button = fake_send_image_with_button
wa_client.download_media = fake_download_media

from app.engine import orchestrator  # noqa: E402
orchestrator.send_text = fake_send_text
orchestrator.send_image = fake_send_image
orchestrator.send_image_with_button = fake_send_image_with_button
orchestrator.download_media = fake_download_media

from app.engine import intent as intent_engine  # noqa: E402


async def fake_classify_intent(user_message):
    # Deliberately return a SHORT, different brief than the raw message --
    # if prompt_builder ends up using this instead of the raw text, the
    # test below will catch it.
    return {"intent": "GENERATE", "brief": "SUMMARIZED-AND-WRONG"}


intent_engine.classify = fake_classify_intent
orchestrator.intent_engine.classify = fake_classify_intent

from app.engine import concept_proposal  # noqa: E402


async def fake_decide(ctx, user_message):
    return {"decision": "SPECIFIC_ENOUGH", "brief": "SUMMARIZED-AND-WRONG"}


concept_proposal.decide = fake_decide
orchestrator.concept_proposal.decide = fake_decide

from app.engine import revision_classifier  # noqa: E402


async def fake_classify_revision(user_message):
    return {"revision_type": "FULL_REGENERATION", "brief": "SUMMARIZED-AND-WRONG"}


revision_classifier.classify = fake_classify_revision
orchestrator.revision_classifier.classify = fake_classify_revision

from app.engine import prompt_builder  # noqa: E402

prompt_builder_calls = []


async def fake_build(ctx, user_brief):
    prompt_builder_calls.append(user_brief)
    return {"image_prompt": f"creative: {user_brief}", "headline_text": "Sale", "notes_for_caption": user_brief}


prompt_builder.build = fake_build
orchestrator.prompt_builder.build = fake_build

from app.engine import image_gen  # noqa: E402

image_gen_calls = []


async def fake_generate_images(prompt, count=2, reference_image=None):
    image_gen_calls.append({"prompt": prompt, "reference_image": reference_image})
    return [png_bytes(), png_bytes()]


image_gen.generate_images = fake_generate_images
orchestrator.image_gen.generate_images = fake_generate_images

from app.engine import quality  # noqa: E402


async def fake_score_and_pick(images):
    return {"best_index": 0, "best_score": 90, "issues": []}


quality.score_and_pick = fake_score_and_pick
orchestrator.quality.score_and_pick = fake_score_and_pick

from app.engine import caption as caption_engine  # noqa: E402


async def fake_caption_generate(ctx, notes_for_caption):
    return {"caption": "Great offer!", "hashtags": "#offer"}


caption_engine.generate = fake_caption_generate
orchestrator.caption_engine.generate = fake_caption_generate


def fake_upload_creative(business_id, generation_id, image_bytes):
    return f"https://fake.example.com/creatives/{generation_id}.png"


def fake_upload_base_image(business_id, generation_id, image_bytes):
    return f"https://fake.example.com/creatives/{generation_id}_base.png"


orchestrator.upload_creative = fake_upload_creative
orchestrator.upload_base_image = fake_upload_base_image

from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402
from app.engine.orchestrator import generate  # noqa: E402


def _make_business(phone):
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="restaurant", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        db.add(BrandProfile(business_id=biz_id, tone="bold"))
        return biz_id


async def run():
    print("=" * 60)
    print("TEST 1: SPECIFIC_ENOUGH path -- prompt_builder gets the raw message, not the summarized brief")
    print("=" * 60)
    prompt_builder_calls.clear()
    phone = "919999999930"
    biz_id = _make_business(phone)
    raw_request = "Change the background to black and add a 25% off overlay"

    await generate(biz_id, IncomingMessage(sender=phone, type="text", text=raw_request))

    assert len(prompt_builder_calls) == 1, f"FAIL: expected exactly one prompt_builder call, got {prompt_builder_calls}"
    assert prompt_builder_calls[0] == raw_request, (
        f"FAIL: expected prompt_builder to receive the client's raw words, got {prompt_builder_calls[0]!r}"
    )
    print(f"PASS: prompt_builder received the raw instruction verbatim: {prompt_builder_calls[0]!r}\n")

    print("=" * 60)
    print("TEST 2: REVISE path -- prompt_builder's revision text is the raw message, not revision_classifier's brief")
    print("=" * 60)
    prompt_builder_calls.clear()

    async def fake_classify_revise_intent(user_message):
        return {"intent": "REVISE", "brief": "SUMMARIZED-AND-WRONG"}

    intent_engine.classify = fake_classify_revise_intent
    orchestrator.intent_engine.classify = fake_classify_revise_intent

    revise_request = "Make the headline bigger and change it to Diwali Sale"
    await generate(biz_id, IncomingMessage(sender=phone, type="text", text=revise_request))

    assert len(prompt_builder_calls) == 1, f"FAIL: expected exactly one prompt_builder call, got {prompt_builder_calls}"
    assert revise_request in prompt_builder_calls[0], (
        f"FAIL: expected the raw revision request in the prompt, got {prompt_builder_calls[0]!r}"
    )
    assert "SUMMARIZED-AND-WRONG" not in prompt_builder_calls[0], (
        f"FAIL: the stale summarized brief leaked into the prompt: {prompt_builder_calls[0]!r}"
    )
    print(f"PASS: revision prompt used the raw request: {prompt_builder_calls[0]!r}\n")

    print("=" * 60)
    print("TEST 3: an uploaded photo is threaded through to image_gen as a reference, not dropped")
    print("=" * 60)
    intent_engine.classify = fake_classify_intent
    orchestrator.intent_engine.classify = fake_classify_intent
    image_gen_calls.clear()
    phone3 = "919999999931"
    biz_id3 = _make_business(phone3)

    await generate(biz_id3, IncomingMessage(
        sender=phone3, type="image", media_id="media-product-1",
        text="Change the background to black and add a 25% off overlay",
    ))

    assert len(image_gen_calls) == 1, f"FAIL: expected exactly one image_gen call, got {image_gen_calls}"
    assert image_gen_calls[0]["reference_image"] == b"FAKE-PRODUCT-PHOTO-BYTES", (
        f"FAIL: expected the uploaded photo passed through as a reference image, got {image_gen_calls[0]}"
    )
    print("PASS: uploaded photo reached image_gen as a reference instead of being silently dropped\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
