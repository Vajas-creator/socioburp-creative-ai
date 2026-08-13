"""
Test for the carousel feature (app/engine/orchestrator.py's
generate_carousel(), app/router.py's "carousel" keyword trigger, and
app/instagram.py's carousel posting branch, covered separately in
test_instagram_posting.py).

Root cause from the Aug 2026 live-test report, item 5 ("carousel format
not generating"): the Make.com scenario already had a working
CreateCarouselPhoto branch (content_type == "carousel") -- nothing on the
app side ever produced a request shaped that way. This fixes the app
side: a message containing "carousel" now generates CAROUSEL_SLIDE_COUNT
(3) related images, uploads each as its own slide, stores them on
Generation.carousel_image_urls, and delivers them to WhatsApp.

Covers:
  - router.py routes a "carousel" message to generate_carousel(), not the
    normal generate() pipeline.
  - Insufficient credits for the carousel's cost (3, not 1) blocks it with
    a carousel-specific message, even though a plain single-credit check
    would have passed.
  - generate_carousel() calls prompt_builder/image_gen exactly
    CAROUSEL_SLIDE_COUNT times, uploads each slide, and saves them (in
    order) on Generation.carousel_image_urls, with image_url set to the
    first slide.
  - Exactly CAROUSEL_CREDIT_COST credits are charged, once.
  - A failure partway through (e.g. image_gen returns nothing for one
    slide) is caught -- the client gets a reply, not silence, and nothing
    is charged.
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_carousel_generation.db"
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


sent_buttons = []


async def fake_send_buttons(to, body, buttons):
    sent_buttons.append(body)


wa_client.send_text = fake_send_text
wa_client.send_image = fake_send_image
wa_client.send_image_with_button = fake_send_image_with_button
wa_client.send_buttons = fake_send_buttons

from app import router, payments  # noqa: E402
router.send_text = fake_send_text
payments.send_buttons = fake_send_buttons

from app.engine import orchestrator as orch  # noqa: E402
orch.send_text = fake_send_text
orch.send_image = fake_send_image
orch.send_image_with_button = fake_send_image_with_button

from app.engine import prompt_builder  # noqa: E402

prompt_builder_calls = []


async def fake_build(ctx, user_brief):
    prompt_builder_calls.append(user_brief)
    return {"image_prompt": f"prompt: {user_brief}", "headline_text": "Sale", "notes_for_caption": user_brief}


prompt_builder.build = fake_build
orch.prompt_builder.build = fake_build

from app.engine import image_gen  # noqa: E402

image_gen_calls = []


async def fake_generate_images(prompt, count=2, reference_image=None):
    image_gen_calls.append(prompt)
    return [png_bytes()] * count


image_gen.generate_images = fake_generate_images
orch.image_gen.generate_images = fake_generate_images

from app.engine import caption as caption_engine  # noqa: E402


async def fake_caption_generate(ctx, notes_for_caption):
    return {"caption": "Great carousel!", "hashtags": "#offer"}


caption_engine.generate = fake_caption_generate
orch.caption_engine.generate = fake_caption_generate

upload_calls = []


def fake_upload_carousel_slide(business_id, generation_id, slide_num, image_bytes):
    url = f"https://fake.example.com/creatives/{generation_id}_slide{slide_num}.png"
    upload_calls.append(url)
    return url


orch.upload_carousel_slide = fake_upload_carousel_slide

from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile, Generation  # noqa: E402
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


async def run():
    print("=" * 60)
    print("TEST 1: 'carousel' keyword routes through router.py to generate_carousel(), not the normal pipeline")
    print("=" * 60)
    generate_calls = []

    async def fake_generate(business_id, msg):
        generate_calls.append(msg.text)

    orch.generate = fake_generate

    phone = "919999999950"
    biz_id = _make_business(phone)
    sent_texts.clear()
    sent_images.clear()
    image_gen_calls.clear()
    upload_calls.clear()

    await router._process_message(biz_id, IncomingMessage(sender=phone, type="text", text="make me a carousel about our weekend offers"))

    assert generate_calls == [], f"FAIL: the normal generate() pipeline should NOT have run for a carousel request, got {generate_calls}"
    assert len(image_gen_calls) == orch.CAROUSEL_SLIDE_COUNT, (
        f"FAIL: expected exactly {orch.CAROUSEL_SLIDE_COUNT} image_gen calls (one per slide), got {len(image_gen_calls)}"
    )
    print(f"PASS: carousel request bypassed generate(), ran generate_carousel() with {len(image_gen_calls)} slide calls\n")

    print("=" * 60)
    print("TEST 2: each slide's built prompt is tagged with its slide number and the shared carousel theme")
    print("=" * 60)
    assert len(prompt_builder_calls) == orch.CAROUSEL_SLIDE_COUNT
    for i, brief in enumerate(prompt_builder_calls, start=1):
        assert f"Slide {i} of {orch.CAROUSEL_SLIDE_COUNT}" in brief, f"FAIL: expected slide numbering in the brief, got {brief!r}"
        assert "weekend offers" in brief, f"FAIL: expected the original request folded into every slide's brief, got {brief!r}"
    print("PASS: each slide's brief carries its slide number and the shared theme\n")

    print("=" * 60)
    print("TEST 3: all 3 slides uploaded, stored on carousel_image_urls (in order), image_url = first slide")
    print("=" * 60)
    assert len(upload_calls) == orch.CAROUSEL_SLIDE_COUNT
    with get_session() as db:
        gen = db.query(Generation).filter(Generation.business_id == biz_id, Generation.trigger_source == "carousel").first()
        assert gen is not None, "FAIL: expected a Generation row with trigger_source='carousel'"
        assert gen.status == "done", f"FAIL: expected status='done', got {gen.status}"
        assert gen.carousel_image_urls == upload_calls, (
            f"FAIL: expected carousel_image_urls to match the uploaded slide URLs in order, got {gen.carousel_image_urls} vs {upload_calls}"
        )
        assert gen.image_url == upload_calls[0], f"FAIL: expected image_url to be the first slide, got {gen.image_url}"
        assert gen.credits_charged == orch.CAROUSEL_CREDIT_COST
    print(f"PASS: 3 slides uploaded and stored correctly, image_url = first slide: {upload_calls[0]!r}\n")

    print("=" * 60)
    print("TEST 4: exactly CAROUSEL_CREDIT_COST credits charged, once")
    print("=" * 60)
    balance = get_balance(biz_id)
    assert balance == 20 - orch.CAROUSEL_CREDIT_COST, f"FAIL: expected {20 - orch.CAROUSEL_CREDIT_COST} credits left, got {balance}"
    print(f"PASS: {orch.CAROUSEL_CREDIT_COST} credits charged, {balance} left\n")

    print("=" * 60)
    print("TEST 5: delivery -- all slides sent as images, success message sent")
    print("=" * 60)
    assert len(sent_images) == orch.CAROUSEL_SLIDE_COUNT, f"FAIL: expected all {orch.CAROUSEL_SLIDE_COUNT} slides delivered, got {len(sent_images)}"
    assert any("carousel" in t.lower() for t in sent_texts), f"FAIL: expected a carousel completion message, got {sent_texts}"
    print(f"PASS: {len(sent_images)} slides delivered, completion message sent\n")

    print("=" * 60)
    print("TEST 6: insufficient credits (< CAROUSEL_CREDIT_COST) blocks with a carousel-specific message, generate_carousel() never runs")
    print("=" * 60)
    phone2 = "919999999951"
    biz_id2 = _make_business(phone2, credits_amount=2)  # enough for a normal post (needs 1), not a carousel (needs 3)
    sent_texts.clear()
    sent_buttons.clear()
    image_gen_calls.clear()

    await router._process_message(biz_id2, IncomingMessage(sender=phone2, type="text", text="carousel for our new menu"))

    assert len(image_gen_calls) == 0, f"FAIL: generate_carousel() should not have run at all, got {len(image_gen_calls)} image_gen calls"
    assert any("carousel" in b.lower() and "credit" in b.lower() for b in sent_buttons), (
        f"FAIL: expected a carousel-specific insufficient-credits message (sent via the topup buttons prompt), got {sent_buttons}"
    )
    with get_session() as db:
        balance2 = get_balance(biz_id2)
        assert balance2 == 2, f"FAIL: no credits should have been touched, got {balance2}"
    print(f"PASS: blocked before running, no credits touched: {[b for b in sent_buttons if 'carousel' in b.lower()]}\n")

    print("=" * 60)
    print("TEST 7: a failure mid-carousel (no image returned for a slide) is caught -- reply sent, nothing charged")
    print("=" * 60)
    phone3 = "919999999952"
    biz_id3 = _make_business(phone3, credits_amount=20)
    sent_texts.clear()

    call_count = {"n": 0}

    async def fake_generate_images_fails_second(prompt, count=2, reference_image=None):
        call_count["n"] += 1
        if call_count["n"] == 2:
            return []  # simulate the "no candidates" failure on slide 2
        return [png_bytes()] * count

    image_gen.generate_images = fake_generate_images_fails_second
    orch.image_gen.generate_images = fake_generate_images_fails_second

    await orch.generate_carousel(biz_id3, IncomingMessage(sender=phone3, type="text", text="carousel about our new branch"))

    assert any("went wrong" in t.lower() for t in sent_texts), f"FAIL: expected the client to get a failure reply, got {sent_texts}"
    balance3 = get_balance(biz_id3)
    assert balance3 == 20, f"FAIL: expected no credits charged on a failed carousel, got {balance3}"
    print(f"PASS: failure caught, client notified, no charge: {sent_texts}\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
