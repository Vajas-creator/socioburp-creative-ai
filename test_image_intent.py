"""
Test for app/engine/image_intent.py -- the "what would you like me to do
with this image?" negotiation for an uploaded photo with NO accompanying
instruction.

Root cause from the Aug 2026 live-test report, item 2 ("uploaded reference
images are being ignored"): previously an image with no caption hit
orchestrator.generate()'s very first line (`if not msg.text: ... return`)
and was silently discarded -- never even downloaded, no acknowledgment.
This intercepts that case in app/router.py before generate() is ever
reached, persists the photo, and asks what to do with it.

Covers:
  - A photo with NO caption is intercepted, persisted, and gets a 3-button
    question -- generate() is never called, nothing is silently dropped.
  - A photo WITH a caption is UNCHANGED -- still goes straight to
    generate() as before (this negotiation only applies to the no-caption
    case).
  - "Use as-is" delivers the uploaded photo directly as the creative (no
    image generation at all), captioned and charged like any other post.
  - "Change background" asks a follow-up, then generates using the photo
    as the reference/base with that specific instruction.
  - "Something else" asks a general follow-up, then generates the same way.
  - Typing an instruction directly (instead of tapping any button) skips
    the extra round-trip and generates immediately -- kept as seamless as
    the "don't want much effort from the customer" product direction asks
    for, without ever guessing what to do.
  - "cancel" at any stage clears the negotiation without generating.
  - Insufficient credits blocks with a topup prompt instead of generating.
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_image_intent.db"
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


REFERENCE_PHOTO_BYTES = png_bytes()

from app.whatsapp import client as wa_client  # noqa: E402

sent_texts, sent_images, sent_buttons_calls = [], [], []


async def fake_send_text(to, body):
    sent_texts.append(body)


async def fake_send_image(to, image_url, caption=""):
    sent_images.append(image_url)


async def fake_send_image_with_button(to, image_url, body, button_id, button_label):
    sent_images.append(image_url)


async def fake_send_buttons(to, body, buttons):
    sent_buttons_calls.append({"body": body, "buttons": buttons})


async def fake_download_media(media_id):
    return REFERENCE_PHOTO_BYTES


wa_client.send_text = fake_send_text
wa_client.send_image = fake_send_image
wa_client.send_image_with_button = fake_send_image_with_button
wa_client.send_buttons = fake_send_buttons
wa_client.download_media = fake_download_media

from app import router, payments  # noqa: E402
router.send_text = fake_send_text
payments.send_buttons = fake_send_buttons

from app.engine import orchestrator as orch  # noqa: E402
orch.send_text = fake_send_text
orch.send_image = fake_send_image
orch.send_image_with_button = fake_send_image_with_button

from app.engine import image_intent  # noqa: E402
image_intent.send_text = fake_send_text
image_intent.send_buttons = fake_send_buttons
image_intent.download_media = fake_download_media

reference_upload_calls = []


def fake_upload_reference_image(business_id, image_bytes):
    url = f"https://fake.example.com/references/{business_id}/{len(reference_upload_calls)}.png"
    reference_upload_calls.append(url)
    return url


image_intent.upload_reference_image = fake_upload_reference_image

creative_upload_calls = []


def fake_upload_creative(business_id, generation_id, image_bytes):
    url = f"https://fake.example.com/creatives/{generation_id}.png"
    creative_upload_calls.append(url)
    return url


image_intent.upload_creative = fake_upload_creative

from app.engine import caption as caption_engine  # noqa: E402


async def fake_caption_generate(ctx, notes_for_caption):
    return {"caption": "Nice shot!", "hashtags": "#business"}


caption_engine.generate = fake_caption_generate
orch.caption_engine.generate = fake_caption_generate

run_generation_calls = []


async def fake_run_generation(business_id, phone, ctx, brief, user_message, last_generation_id, is_revision, trigger_source=None, reference_image=None):
    run_generation_calls.append({
        "brief": brief, "is_revision": is_revision, "trigger_source": trigger_source, "reference_image": reference_image,
    })


orch._run_generation = fake_run_generation

generate_calls = []


async def fake_generate(business_id, msg):
    generate_calls.append(msg.text)


orch.generate = fake_generate

# Fake a real HTTP fetch of the persisted reference photo (image_intent.py
# re-downloads it from the stored R2 URL before using it) -- the fake
# upload URLs aren't real, and this sandbox blocks outbound HTTP anyway.


class _FakeRefResponse:
    status_code = 200
    content = REFERENCE_PHOTO_BYTES


class _FakeRefHttpClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        return _FakeRefResponse()


image_intent.httpx.AsyncClient = lambda *a, **kw: _FakeRefHttpClient()

from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile, ConversationState, Generation  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402
from app.credits import add_credits, get_balance  # noqa: E402


def _make_business(phone, credits_amount=20):
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="restaurant", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        db.add(BrandProfile(business_id=biz_id, tone="bold"))
        add_credits(db, biz_id, credits_amount, reason="signup_bonus")
        return biz_id


def _pending(biz_id):
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == biz_id).first()
        return convo.pending_image_intent if convo else None


async def run():
    print("=" * 60)
    print("TEST 1: a photo with NO caption is intercepted -- persisted, asked what to do, generate() never runs")
    print("=" * 60)
    phone = "919999999970"
    biz_id = _make_business(phone)
    sent_buttons_calls.clear()
    reference_upload_calls.clear()
    generate_calls.clear()

    await router._process_message(biz_id, IncomingMessage(sender=phone, type="image", media_id="media-1", text=None))

    assert len(reference_upload_calls) == 1, f"FAIL: expected the photo persisted immediately, got {reference_upload_calls}"
    assert len(sent_buttons_calls) == 1, f"FAIL: expected exactly one 3-button question, got {sent_buttons_calls}"
    button_ids = [bid for bid, _ in sent_buttons_calls[0]["buttons"]]
    assert button_ids == ["img_change_bg", "img_use_as_is", "img_something_else"], f"FAIL: unexpected buttons {button_ids}"
    assert generate_calls == [], "FAIL: generate() must not run for a captionless image"
    assert _pending(biz_id) is not None
    print(f"PASS: photo persisted, asked what to do: {sent_buttons_calls[0]['body']!r}\n")

    print("=" * 60)
    print("TEST 2: a photo WITH a caption is unaffected -- still goes straight to generate() as before")
    print("=" * 60)
    phone1b = "919999999980"  # fresh business -- no pending negotiation to interfere
    biz_id1b = _make_business(phone1b)
    generate_calls.clear()
    sent_buttons_calls.clear()
    await router._process_message(biz_id1b, IncomingMessage(sender=phone1b, type="image", media_id="media-2", text="Change the background to black"))
    assert generate_calls == ["Change the background to black"], f"FAIL: expected generate() called with the caption, got {generate_calls}"
    assert sent_buttons_calls == [], "FAIL: a captioned image should never trigger the image-intent question"
    print("PASS: captioned image unaffected, still reaches generate() directly\n")

    print("=" * 60)
    print("TEST 3: 'Use as-is' delivers the uploaded photo directly -- no image generation, charged, delivered")
    print("=" * 60)
    phone2 = "919999999971"
    biz_id2 = _make_business(phone2)
    creative_upload_calls.clear()
    sent_images.clear()
    sent_texts.clear()

    await router._process_message(biz_id2, IncomingMessage(sender=phone2, type="image", media_id="media-3", text=None))
    await router._process_message(biz_id2, IncomingMessage(sender=phone2, type="button", button_id="img_use_as_is", text="Use as-is"))

    assert len(creative_upload_calls) == 1, f"FAIL: expected the photo uploaded as the final creative, got {creative_upload_calls}"
    assert run_generation_calls == [], "FAIL: 'use as-is' must not go through the image-generation pipeline at all"
    with get_session() as db:
        gen = db.query(Generation).filter(Generation.business_id == biz_id2).first()
        assert gen is not None and gen.status == "done"
        assert gen.credits_charged == 1
        assert gen.trigger_source == "image_intent_as_is"
    assert get_balance(biz_id2) == 19, f"FAIL: expected 1 credit charged, got balance {get_balance(biz_id2)}"
    assert len(sent_images) == 1, f"FAIL: expected the photo delivered, got {sent_images}"
    assert _pending(biz_id2) is None
    print("PASS: 'Use as-is' delivered the raw photo directly, no generation pipeline involved\n")

    print("=" * 60)
    print("TEST 4: 'Change background' asks a follow-up, then generates with that instruction + the photo as reference")
    print("=" * 60)
    phone3 = "919999999972"
    biz_id3 = _make_business(phone3)
    run_generation_calls.clear()
    sent_texts.clear()

    await router._process_message(biz_id3, IncomingMessage(sender=phone3, type="image", media_id="media-4", text=None))
    await router._process_message(biz_id3, IncomingMessage(sender=phone3, type="button", button_id="img_change_bg", text="Change background"))
    assert any("new background" in t.lower() for t in sent_texts), f"FAIL: expected the background follow-up question, got {sent_texts}"

    await router._process_message(biz_id3, IncomingMessage(sender=phone3, type="text", text="a sunny beach"))

    assert len(run_generation_calls) == 1, f"FAIL: expected exactly one generation call, got {run_generation_calls}"
    call = run_generation_calls[0]
    assert "sunny beach" in call["brief"], f"FAIL: expected the background instruction in the brief, got {call['brief']!r}"
    assert call["reference_image"] == REFERENCE_PHOTO_BYTES, "FAIL: expected the uploaded photo used as the reference image"
    assert call["trigger_source"] == "image_intent"
    assert _pending(biz_id3) is None
    print(f"PASS: generated with instruction {call['brief']!r} using the uploaded photo as reference\n")

    print("=" * 60)
    print("TEST 5: 'Something else' asks a general follow-up, then generates the same way")
    print("=" * 60)
    phone4 = "919999999973"
    biz_id4 = _make_business(phone4)
    run_generation_calls.clear()
    sent_texts.clear()

    await router._process_message(biz_id4, IncomingMessage(sender=phone4, type="image", media_id="media-5", text=None))
    await router._process_message(biz_id4, IncomingMessage(sender=phone4, type="button", button_id="img_something_else", text="Something else"))
    assert any("what would you like me to do" in t.lower() for t in sent_texts), f"FAIL: expected the general follow-up, got {sent_texts}"

    await router._process_message(biz_id4, IncomingMessage(sender=phone4, type="text", text="add a 20% off banner"))

    assert len(run_generation_calls) == 1
    assert "20% off" in run_generation_calls[0]["brief"]
    assert run_generation_calls[0]["reference_image"] == REFERENCE_PHOTO_BYTES
    print("PASS: 'Something else' -> follow-up -> generated with the given instruction\n")

    print("=" * 60)
    print("TEST 6: typing an instruction directly (no button tap) skips the extra round-trip")
    print("=" * 60)
    phone5 = "919999999974"
    biz_id5 = _make_business(phone5)
    run_generation_calls.clear()
    sent_texts.clear()

    await router._process_message(biz_id5, IncomingMessage(sender=phone5, type="image", media_id="media-6", text=None))
    await router._process_message(biz_id5, IncomingMessage(sender=phone5, type="text", text="Make it look more premium"))

    assert len(run_generation_calls) == 1, f"FAIL: expected immediate generation without an extra round-trip, got {run_generation_calls}"
    assert "premium" in run_generation_calls[0]["brief"]
    assert run_generation_calls[0]["reference_image"] == REFERENCE_PHOTO_BYTES
    print("PASS: typed instruction skipped the extra round-trip, generated immediately\n")

    print("=" * 60)
    print("TEST 7: 'cancel' at any stage clears the negotiation without generating")
    print("=" * 60)
    phone6 = "919999999975"
    biz_id6 = _make_business(phone6)
    run_generation_calls.clear()
    sent_texts.clear()

    await router._process_message(biz_id6, IncomingMessage(sender=phone6, type="image", media_id="media-7", text=None))
    await router._process_message(biz_id6, IncomingMessage(sender=phone6, type="text", text="cancel"))

    assert _pending(biz_id6) is None
    assert any("no worries" in t.lower() for t in sent_texts), f"FAIL: expected a cancellation acknowledgment, got {sent_texts}"
    assert run_generation_calls == []
    print("PASS: cancel cleared the negotiation cleanly\n")

    print("=" * 60)
    print("TEST 8: insufficient credits blocks with a topup prompt instead of generating")
    print("=" * 60)
    phone7 = "919999999976"
    biz_id7 = _make_business(phone7, credits_amount=0)
    run_generation_calls.clear()
    sent_buttons_calls.clear()

    await router._process_message(biz_id7, IncomingMessage(sender=phone7, type="image", media_id="media-8", text=None))
    await router._process_message(biz_id7, IncomingMessage(sender=phone7, type="button", button_id="img_use_as_is", text="Use as-is"))

    assert run_generation_calls == []
    assert any("credit" in b["body"].lower() for b in sent_buttons_calls), f"FAIL: expected a topup prompt, got {sent_buttons_calls}"
    print("PASS: insufficient credits blocked with a topup prompt\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
