"""
Test for the carousel feature: app/engine/carousel.py's multi-turn
negotiation (how many slides, what each one shows) and
app/engine/orchestrator.py's generate_carousel() (the actual generation,
called once the negotiation completes).

Root cause from the Aug 2026 live-test report, item 5: the previous
carousel implementation generated a fixed 3 slides with no per-slide
content and never asked the user anything, and separately a report of a
single collage image with baked-in "3/5, 4/5, 5/5" panel labels indicated
some carousel-shaped requests were falling through to the old
single-image pipeline entirely. This replaces the fixed-3-slide design
with a real negotiation: pick a slide count (1-9, via a WhatsApp list
message) and describe each slide, THEN generate -- N genuinely separate
images, never a collage.

Covers:
  - "carousel" in a message starts the negotiation (a list message asking
    for a count), not generation.
  - Picking a count via the list reply (button_id="carousel_count_N")
    advances to asking for slide content.
  - Picking a count via a plain typed digit also works (fallback for
    clients who type instead of tapping).
  - An out-of-range/non-numeric reply re-prompts instead of advancing.
  - Once slide content is given, generate_carousel() runs with exactly N
    slide briefs, N separate image_gen calls, N separately uploaded
    files, and Generation.carousel_image_urls holding all N URLs in order.
  - N=1 delivers/posts as a normal single photo (carousel_image_urls
    stays None) -- Instagram's carousel API needs >=2 items.
  - Credits charged = N (1 per slide), checked against the actual balance
    BEFORE generating, not a fixed cost.
  - A photo attached to the opening "carousel" message is persisted and
    reused as the reference/base image for every slide.
  - "cancel" at any stage clears the negotiation without generating.
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

sent_texts, sent_images, sent_lists = [], [], []


async def fake_send_text(to, body):
    sent_texts.append(body)


async def fake_send_image(to, image_url, caption=""):
    sent_images.append(image_url)


async def fake_send_image_with_button(to, image_url, body, button_id, button_label):
    sent_images.append(image_url)


async def fake_send_list(to, body, button_text, rows, section_title="Options"):
    sent_lists.append({"body": body, "rows": rows})


async def fake_download_media(media_id):
    return b"FAKE-PHOTO-BYTES"


wa_client.send_text = fake_send_text
wa_client.send_image = fake_send_image
wa_client.send_image_with_button = fake_send_image_with_button
wa_client.send_list = fake_send_list
wa_client.download_media = fake_download_media

sent_buttons = []


async def fake_send_buttons(to, body, buttons):
    sent_buttons.append(body)


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

from app.engine import orchestrator as orch  # noqa: E402

async def _fake_content_policy_check(text):
    return {"allowed": True, "reason": None}

orch.content_policy.check = _fake_content_policy_check
orch.send_text = fake_send_text
orch.send_image = fake_send_image
orch.send_image_with_button = fake_send_image_with_button

from app.engine import carousel  # noqa: E402
carousel.send_text = fake_send_text
carousel.send_list = fake_send_list
carousel.download_media = fake_download_media

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
    image_gen_calls.append({"prompt": prompt, "reference_image": reference_image})
    return [png_bytes()] * count


image_gen.generate_images = fake_generate_images
orch.image_gen.generate_images = fake_generate_images

from app.engine import quality  # noqa: E402


async def fake_score_and_pick(images):
    return {"best_index": 0, "best_score": 90, "issues": []}


quality.score_and_pick = fake_score_and_pick
orch.quality.score_and_pick = fake_score_and_pick

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

reference_upload_calls = []


def fake_upload_reference_image(business_id, image_bytes):
    url = f"https://fake.example.com/references/{business_id}/{len(reference_upload_calls)}.png"
    reference_upload_calls.append(url)
    return url


carousel.upload_reference_image = fake_upload_reference_image

# Fake the two distinct Claude calls carousel.py makes -- deterministic
# extraction so assertions don't depend on real model behavior.
#
# 1. COMBINED_SYSTEM_PROMPT (carousel._infer_count_and_slides, run on the
#    OPENING "carousel" message to decide what can be skipped): this fake
#    uses an explicit, test-controlled convention -- a colon splits an
#    optional leading count from a comma-separated slide list, e.g.
#    "3-image carousel: product shot, lifestyle, pricing" -> count=3,
#    slides=[...]. No colon and no digit -> both null (genuinely vague,
#    matching real Claude's expected behavior for e.g. "make me a carousel
#    about our weekend menu").
# 2. SLIDES_SYSTEM_PROMPT (carousel._parse_slide_briefs, run once slide
#    content is actually given): naive comma/newline split, padded or
#    truncated to the requested count.


async def fake_create_message(model, max_tokens, system, messages):
    import json as _json
    import re as _re

    class _Resp:
        def __init__(self, text):
            self.content = [type("C", (), {"text": text})()]

    raw = messages[0]["content"]

    if "count: an explicit number" in system:
        count = None
        m = _re.search(r"\b(\d+)[\s-]*(?:images?|slides?)\b", raw, _re.IGNORECASE)
        if m:
            count = int(m.group(1))
        slides = None
        if ":" in raw:
            after_colon = raw.split(":", 1)[1]
            items = [p.strip() for p in after_colon.split(",") if p.strip()]
            slides = items or None
        return _Resp(_json.dumps({"count": count, "slides": slides}))

    # SLIDES_SYSTEM_PROMPT path
    parts = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
    m = _re.search(r"EXACTLY (\d+)", system)
    count = int(m.group(1)) if m else len(parts)
    while len(parts) < count:
        parts.append(parts[-1] if parts else raw)
    parts = parts[:count]
    return _Resp(_json.dumps({"slides": parts}))


carousel.create_message = fake_create_message

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


def _pending_carousel(biz_id):
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == biz_id).first()
        return convo.pending_carousel if convo else None


async def run():
    print("=" * 60)
    print("TEST 1: 'carousel' keyword starts the negotiation -- asks for a count, does NOT generate")
    print("=" * 60)
    phone = "919999999960"
    biz_id = _make_business(phone)
    sent_lists.clear()
    image_gen_calls.clear()

    await router._process_message(biz_id, IncomingMessage(sender=phone, type="text", text="I want a carousel about our weekend menu"))

    assert len(sent_lists) == 1, f"FAIL: expected exactly one list message asking for a count, got {sent_lists}"
    row_ids = [rid for rid, _ in sent_lists[0]["rows"]]
    assert row_ids == [f"carousel_count_{n}" for n in range(1, 10)], f"FAIL: expected rows for 1-9, got {row_ids}"
    assert len(image_gen_calls) == 0, f"FAIL: nothing should be generated yet, got {image_gen_calls}"
    pending = _pending_carousel(biz_id)
    assert pending is not None, "FAIL: expected pending_carousel to be set"
    print(f"PASS: carousel negotiation started, {len(row_ids)} count options offered, nothing generated yet\n")

    print("=" * 60)
    print("TEST 2: an out-of-range/non-numeric reply re-prompts, stays in awaiting_count")
    print("=" * 60)
    sent_texts.clear()
    sent_lists.clear()
    await router._process_message(biz_id, IncomingMessage(sender=phone, type="text", text="lots"))
    assert any("between" in t.lower() for t in sent_texts), f"FAIL: expected a re-prompt, got {sent_texts}"
    assert len(sent_lists) == 1, "FAIL: expected the count list to be re-sent"
    import json
    pending = json.loads(_pending_carousel(biz_id))
    assert pending["stage"] == "awaiting_count", f"FAIL: expected still awaiting_count, got {pending}"
    print("PASS: invalid count re-prompted without advancing\n")

    print("=" * 60)
    print("TEST 3: picking a count via the list reply (button_id) advances to asking for slide content")
    print("=" * 60)
    sent_texts.clear()
    await router._process_message(biz_id, IncomingMessage(sender=phone, type="button", button_id="carousel_count_3", text="3 images"))
    pending = json.loads(_pending_carousel(biz_id))
    assert pending["stage"] == "awaiting_slide_content", f"FAIL: expected awaiting_slide_content, got {pending}"
    assert pending["count"] == 3, f"FAIL: expected count=3, got {pending}"
    assert any("what should each slide" in t.lower() for t in sent_texts), f"FAIL: expected the slide-content question, got {sent_texts}"
    print("PASS: count selected via list reply, now asking for slide content\n")

    print("=" * 60)
    print("TEST 4: providing slide content generates exactly 3 separate images, uploaded and stored correctly")
    print("=" * 60)
    image_gen_calls.clear()
    upload_calls.clear()
    sent_images.clear()
    sent_texts.clear()

    await router._process_message(biz_id, IncomingMessage(
        sender=phone, type="text", text="product shot, behind-the-scenes, pricing",
    ))

    assert len(image_gen_calls) == 3, f"FAIL: expected exactly 3 separate image_gen calls, got {len(image_gen_calls)}"
    assert len(upload_calls) == 3, f"FAIL: expected 3 separate uploads, got {len(upload_calls)}"
    assert _pending_carousel(biz_id) is None, "FAIL: expected the negotiation to be cleared after generating"

    with get_session() as db:
        gen = db.query(Generation).filter(Generation.business_id == biz_id, Generation.trigger_source == "carousel").order_by(Generation.created_at.desc()).first()
        assert gen is not None
        assert gen.carousel_image_urls == upload_calls, f"FAIL: expected carousel_image_urls to match uploads in order, got {gen.carousel_image_urls}"
        assert gen.image_url == upload_calls[0]
        assert gen.credits_charged == 3, f"FAIL: expected 3 credits charged (1/slide), got {gen.credits_charged}"
    assert len(sent_images) == 3, f"FAIL: expected all 3 slides delivered as images, got {len(sent_images)}"
    assert any("carousel" in t.lower() for t in sent_texts), f"FAIL: expected a carousel completion message, got {sent_texts}"
    print(f"PASS: 3 genuinely separate images generated, uploaded, and delivered: {upload_calls}\n")

    print("=" * 60)
    print("TEST 5: N=1 delivers as a normal single photo -- carousel_image_urls stays None")
    print("=" * 60)
    phone2 = "919999999961"
    biz_id2 = _make_business(phone2)
    image_gen_calls.clear()
    upload_calls.clear()
    sent_images.clear()

    await router._process_message(biz_id2, IncomingMessage(sender=phone2, type="text", text="carousel of our new branch"))
    await router._process_message(biz_id2, IncomingMessage(sender=phone2, type="button", button_id="carousel_count_1", text="1 image (single post)"))
    await router._process_message(biz_id2, IncomingMessage(sender=phone2, type="text", text="our new branch storefront"))

    assert len(image_gen_calls) == 1, f"FAIL: expected exactly 1 image_gen call, got {len(image_gen_calls)}"
    with get_session() as db:
        gen2 = db.query(Generation).filter(Generation.business_id == biz_id2, Generation.trigger_source == "carousel").first()
        assert gen2.carousel_image_urls is None, f"FAIL: expected carousel_image_urls=None for a 1-slide carousel, got {gen2.carousel_image_urls}"
        assert gen2.image_url == upload_calls[0]
        assert gen2.credits_charged == 1
    assert len(sent_images) == 1, f"FAIL: expected exactly 1 image delivered (no 'plain images then last with button' loop), got {len(sent_images)}"
    print("PASS: a 1-slide carousel delivers/stores like a normal single photo\n")

    print("=" * 60)
    print("TEST 6: picking a count via a plain typed digit also works")
    print("=" * 60)
    phone3 = "919999999962"
    biz_id3 = _make_business(phone3)
    await router._process_message(biz_id3, IncomingMessage(sender=phone3, type="text", text="make me a carousel"))
    await router._process_message(biz_id3, IncomingMessage(sender=phone3, type="text", text="2"))
    pending3 = json.loads(_pending_carousel(biz_id3))
    assert pending3["stage"] == "awaiting_slide_content" and pending3["count"] == 2, f"FAIL: expected count=2 parsed from typed digit, got {pending3}"
    print("PASS: typed digit '2' correctly parsed as the slide count\n")

    print("=" * 60)
    print("TEST 7: insufficient credits at count-selection time blocks with a specific message, negotiation cleared")
    print("=" * 60)
    phone4 = "919999999963"
    biz_id4 = _make_business(phone4, credits_amount=2)
    sent_buttons.clear()
    image_gen_calls.clear()
    await router._process_message(biz_id4, IncomingMessage(sender=phone4, type="text", text="carousel please"))
    await router._process_message(biz_id4, IncomingMessage(sender=phone4, type="button", button_id="carousel_count_5", text="5 images"))

    assert len(image_gen_calls) == 0, f"FAIL: should not have generated anything, got {image_gen_calls}"
    assert any("5" in b and "credit" in b.lower() for b in sent_buttons), f"FAIL: expected a specific insufficient-credits message, got {sent_buttons}"
    assert _pending_carousel(biz_id4) is None, "FAIL: expected the negotiation cleared after the credit block"
    print(f"PASS: blocked with a specific message, negotiation cleared: {[b for b in sent_buttons if 'credit' in b.lower()]}\n")

    print("=" * 60)
    print("TEST 8: a photo attached to the opening 'carousel' message is persisted and reused for every slide")
    print("=" * 60)
    phone5 = "919999999964"
    biz_id5 = _make_business(phone5)
    image_gen_calls.clear()
    reference_upload_calls.clear()

    # generate_carousel() fetches the persisted reference image back over
    # HTTP -- fake that fetch (the fake upload URL isn't real, and this
    # sandbox blocks arbitrary outbound HTTP anyway).
    class _FakeRefResponse:
        status_code = 200
        content = b"FAKE-PHOTO-BYTES"

    class _FakeRefHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _FakeRefResponse()

    real_orch_async_client = orch.httpx.AsyncClient
    orch.httpx.AsyncClient = lambda *a, **kw: _FakeRefHttpClient()

    await router._process_message(biz_id5, IncomingMessage(sender=phone5, type="image", media_id="media-1", text="carousel of this product"))
    assert len(reference_upload_calls) == 1, f"FAIL: expected the attached photo persisted immediately, got {reference_upload_calls}"

    await router._process_message(biz_id5, IncomingMessage(sender=phone5, type="button", button_id="carousel_count_2", text="2 images"))
    await router._process_message(biz_id5, IncomingMessage(sender=phone5, type="text", text="front view, side view"))

    orch.httpx.AsyncClient = real_orch_async_client

    assert len(image_gen_calls) == 2
    assert all(c["reference_image"] == b"FAKE-PHOTO-BYTES" for c in image_gen_calls), (
        f"FAIL: expected every slide to use the uploaded photo as the reference image, got {image_gen_calls}"
    )
    print("PASS: the uploaded product photo was used as the reference image for every slide\n")

    print("=" * 60)
    print("TEST 9: 'cancel' at any stage clears the negotiation without generating")
    print("=" * 60)
    phone6 = "919999999965"
    biz_id6 = _make_business(phone6)
    image_gen_calls.clear()
    sent_texts.clear()

    await router._process_message(biz_id6, IncomingMessage(sender=phone6, type="text", text="carousel time"))
    await router._process_message(biz_id6, IncomingMessage(sender=phone6, type="text", text="cancel"))

    assert _pending_carousel(biz_id6) is None, "FAIL: expected the negotiation cleared on cancel"
    assert any("cancel" in t.lower() for t in sent_texts), f"FAIL: expected a cancellation acknowledgment, got {sent_texts}"
    assert len(image_gen_calls) == 0
    print("PASS: cancel cleared the negotiation cleanly\n")

    print("=" * 60)
    print("TEST 10: fully-specified opening message ('3-image carousel: A, B, C') skips BOTH questions entirely")
    print("=" * 60)
    phone7 = "919999999966"
    biz_id7 = _make_business(phone7)
    image_gen_calls.clear()
    upload_calls.clear()
    sent_lists.clear()
    sent_texts.clear()

    await router._process_message(biz_id7, IncomingMessage(
        sender=phone7, type="text",
        text="I want a 3-image carousel: product shot, behind-the-scenes, pricing",
    ))

    assert sent_lists == [], f"FAIL: expected no count question, got {sent_lists}"
    assert _pending_carousel(biz_id7) is None, "FAIL: expected no negotiation left pending -- should have generated immediately"
    assert len(image_gen_calls) == 3, f"FAIL: expected 3 slides generated immediately with zero questions asked, got {len(image_gen_calls)}"
    with get_session() as db:
        gen7 = db.query(Generation).filter(Generation.business_id == biz_id7, Generation.trigger_source == "carousel").first()
        assert gen7.carousel_image_urls == upload_calls
    print("PASS: a fully-specified request generated immediately, no questions asked\n")

    print("=" * 60)
    print("TEST 11: count-only opening message ('5-image carousel') skips the count question, asks only for content")
    print("=" * 60)
    phone8 = "919999999967"
    biz_id8 = _make_business(phone8)
    image_gen_calls.clear()
    sent_lists.clear()
    sent_texts.clear()

    await router._process_message(biz_id8, IncomingMessage(sender=phone8, type="text", text="Make me a 5-image carousel"))

    assert sent_lists == [], f"FAIL: expected the count list to be skipped since count was already stated, got {sent_lists}"
    assert len(image_gen_calls) == 0, "FAIL: should not generate yet -- slide content still needed"
    pending8 = json.loads(_pending_carousel(biz_id8))
    assert pending8["stage"] == "awaiting_slide_content" and pending8["count"] == 5, f"FAIL: expected count=5 already set, awaiting content only, got {pending8}"
    assert any("5 images" in t for t in sent_texts), f"FAIL: expected the content question to reference the already-known count, got {sent_texts}"
    print("PASS: count skipped straight to the one remaining question (slide content)\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
