"""
Test for four fixes found during a deeper audit after the carousel/image-
upload work: the carousel quality gate, the learning/personalization
signal, the "can't revise a carousel" guard, and the "an unambiguous
global command mid-negotiation means switch context" escape hatch.

Covers:
  - Each carousel slide goes through the same quality gate as a single
    image (2 candidates, scored, one regen attempt if below threshold) --
    previously each slide generated exactly 1 candidate with NO scoring
    at all.
  - A low-scoring slide triggers exactly one regen attempt; if the
    regenerated candidate scores better, it's used.
  - Slides are generated concurrently, not one at a time (checked via a
    fake image_gen that records overlapping in-flight calls).
  - generate_carousel() records the tacit-acceptance learning signal for
    whatever was generated right before it -- previously missing, so
    using carousel/photo-upload flows silently stopped the client's
    preference learning.
  - A carousel becomes the new last_generation_id once it completes, and
    a "make it more premium" right after gets a clear "can't edit a
    carousel yet" message instead of silently targeting something else.
  - Typing "credits"/"topup"/"history" mid-carousel-negotiation cancels
    the negotiation and is handled as that command instead of being
    swallowed as a (nonsensical) answer to the pending question.
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_carousel_deep_fixes.db"
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
    sent_lists.append(body)


wa_client.send_text = fake_send_text
wa_client.send_image = fake_send_image
wa_client.send_image_with_button = fake_send_image_with_button
wa_client.send_list = fake_send_list

from app import router  # noqa: E402
router.send_text = fake_send_text

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

# Fake the two Claude calls carousel.py makes for count/slide inference --
# same test-controlled convention as test_carousel_generation.py: a colon
# splits an optional leading count from a comma-separated slide list.


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

    parts = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
    m = _re.search(r"EXACTLY (\d+)", system)
    count = int(m.group(1)) if m else len(parts)
    while len(parts) < count:
        parts.append(parts[-1] if parts else raw)
    parts = parts[:count]
    return _Resp(_json.dumps({"slides": parts}))


carousel.create_message = fake_create_message

from app.engine import prompt_builder  # noqa: E402


async def fake_build(ctx, user_brief):
    return {"image_prompt": f"prompt: {user_brief}", "headline_text": "Sale", "notes_for_caption": user_brief}


prompt_builder.build = fake_build
orch.prompt_builder.build = fake_build

from app.engine import image_gen  # noqa: E402

image_gen_calls = []
in_flight = {"current": 0, "max_seen": 0}
_calls_per_prompt = {}  # prompt -> how many times generate_images has been called for it


async def fake_generate_images(prompt, count=2, reference_image=None):
    in_flight["current"] += 1
    in_flight["max_seen"] = max(in_flight["max_seen"], in_flight["current"])
    await asyncio.sleep(0.02)  # let concurrent calls overlap
    image_gen_calls.append({"prompt": prompt, "count": count})
    in_flight["current"] -= 1
    # Encode which call-number this is FOR THIS SPECIFIC PROMPT into the
    # pixel color, so the fake quality scorer below can deterministically
    # score a slide's FIRST candidate set low (triggering its own regen)
    # and its SECOND (the regen) high -- keyed per-slide, not by global
    # call order, since slides now generate concurrently and interleave.
    _calls_per_prompt[prompt] = _calls_per_prompt.get(prompt, 0) + 1
    call_num = _calls_per_prompt[prompt]
    return [png_bytes(color=(call_num, 50, 50))] * count


image_gen.generate_images = fake_generate_images
orch.image_gen.generate_images = fake_generate_images

from app.engine import quality  # noqa: E402

quality_calls = []


async def fake_score_and_pick(images):
    quality_calls.append(len(images))
    call_num = Image.open(io.BytesIO(images[0])).convert("RGB").getpixel((0, 0))[0]
    if call_num == 1:
        return {"best_index": 0, "best_score": 40, "issues": ["low score, test-forced"]}
    return {"best_index": 0, "best_score": 85, "issues": []}


quality.score_and_pick = fake_score_and_pick
orch.quality.score_and_pick = fake_score_and_pick

from app.engine import caption as caption_engine  # noqa: E402


async def fake_caption_generate(ctx, notes_for_caption):
    return {"caption": "Nice!", "hashtags": "#offer"}


caption_engine.generate = fake_caption_generate
orch.caption_engine.generate = fake_caption_generate

upload_calls = []


def fake_upload_carousel_slide(business_id, generation_id, slide_num, image_bytes):
    url = f"https://fake.example.com/creatives/{generation_id}_slide{slide_num}.png"
    upload_calls.append(url)
    return url


orch.upload_carousel_slide = fake_upload_carousel_slide

record_accepted_calls = []


async def fake_record_accepted_direction(business_id, generation_id, require_quality_threshold=True):
    record_accepted_calls.append((business_id, generation_id))


from app.engine import learning  # noqa: E402
learning.record_accepted_direction = fake_record_accepted_direction
orch.learning.record_accepted_direction = fake_record_accepted_direction
carousel.load_business_context  # noqa: B018 -- just confirming the import path exists

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


async def run():
    print("=" * 60)
    print("TEST 1: each carousel slide is quality-scored (2 candidates), a low score triggers exactly one regen")
    print("=" * 60)
    phone = "919999999950"
    biz_id = _make_business(phone)
    image_gen_calls.clear()
    quality_calls.clear()
    in_flight["current"] = 0
    in_flight["max_seen"] = 0

    await router._process_message(biz_id, IncomingMessage(sender=phone, type="text", text="2-image carousel: product shot, lifestyle shot"))

    # 2 slides, each: 1 initial call (count=2) + 1 regen call (count=2) since fake_score_and_pick always scores the FIRST call low.
    assert len(image_gen_calls) == 4, f"FAIL: expected 4 image_gen calls (2 slides x initial+regen), got {len(image_gen_calls)}"
    assert all(c["count"] == 2 for c in image_gen_calls), f"FAIL: expected count=2 (quality-scored) on every call, got {image_gen_calls}"
    assert len(quality_calls) == 4, f"FAIL: expected score_and_pick called once per image_gen call, got {len(quality_calls)}"
    print(f"PASS: {len(image_gen_calls)} image_gen calls (initial + regen per slide), all quality-scored\n")

    print("=" * 60)
    print("TEST 2: slides generate concurrently, not one at a time")
    print("=" * 60)
    assert in_flight["max_seen"] >= 2, f"FAIL: expected overlapping in-flight image_gen calls (parallel slides), max concurrent seen = {in_flight['max_seen']}"
    print(f"PASS: up to {in_flight['max_seen']} slides generating concurrently\n")

    print("=" * 60)
    print("TEST 3: generate_carousel() records the tacit-acceptance learning signal for the PRIOR generation")
    print("=" * 60)
    phone2 = "919999999951"
    biz_id2 = _make_business(phone2)
    with get_session() as db:
        prior_gen = Generation(business_id=biz_id2, user_message="earlier post", status="done", quality_score=90, credits_charged=1)
        db.add(prior_gen)
        db.flush()
        prior_gen_id = prior_gen.id
        convo = ConversationState(business_id=biz_id2, last_generation_id=prior_gen_id)
        db.add(convo)

    record_accepted_calls.clear()
    await router._process_message(biz_id2, IncomingMessage(sender=phone2, type="text", text="2-image carousel: A, B"))

    assert (biz_id2, prior_gen_id) in record_accepted_calls, (
        f"FAIL: expected the prior generation recorded as tacitly accepted, got {record_accepted_calls}"
    )
    print("PASS: prior generation's acceptance recorded when starting a carousel\n")

    print("=" * 60)
    print("TEST 4: a carousel becomes last_generation_id; a 'make it more premium' after it gets a clear guard message")
    print("=" * 60)
    with get_session() as db:
        convo2 = db.query(ConversationState).filter(ConversationState.business_id == biz_id2).first()
        carousel_gen_id = convo2.last_generation_id
        gen_row = db.query(Generation).filter(Generation.id == carousel_gen_id).first()
        assert gen_row.carousel_image_urls is not None, "FAIL: expected the carousel's own row to be last_generation_id"

    from app.engine import intent as intent_engine

    async def fake_classify_revise(user_message):
        return {"intent": "REVISE", "brief": user_message}

    intent_engine.classify = fake_classify_revise
    orch.intent_engine.classify = fake_classify_revise

    sent_texts.clear()
    from app.engine.orchestrator import generate
    await generate(biz_id2, IncomingMessage(sender=phone2, type="text", text="make it more premium"))

    assert len(sent_texts) == 1, f"FAIL: expected exactly one reply, got {sent_texts}"
    assert "can't edit a carousel" in sent_texts[0].lower(), f"FAIL: expected the carousel-revision guard message, got {sent_texts[0]!r}"
    print(f"PASS: {sent_texts[0]!r}\n")

    print("=" * 60)
    print("TEST 5: 'credits' typed mid-carousel-negotiation cancels it and is handled as the credits command")
    print("=" * 60)
    phone3 = "919999999952"
    biz_id3 = _make_business(phone3, credits_amount=17)
    sent_texts.clear()
    sent_lists.clear()

    await router._process_message(biz_id3, IncomingMessage(sender=phone3, type="text", text="make me a carousel"))
    assert len(sent_lists) == 1, "FAIL: expected the negotiation to actually start (count list sent)"

    await router._process_message(biz_id3, IncomingMessage(sender=phone3, type="text", text="credits"))

    with get_session() as db:
        convo3 = db.query(ConversationState).filter(ConversationState.business_id == biz_id3).first()
        assert convo3.pending_carousel is None, "FAIL: expected the carousel negotiation cancelled"
    assert any("credits remaining" in t.lower() for t in sent_texts), f"FAIL: expected the credits balance reply, got {sent_texts}"
    print(f"PASS: negotiation cancelled, handled as the credits command instead: {sent_texts}\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
