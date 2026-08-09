"""
Test for app/engine/brand_reflection.py (the two new persona-voiced
"reflecting understanding" messages) and their wiring into onboarding.py
and orchestrator.py.

Covers:
  - reflect_understanding()/reflect_first_result() parse a well-formed
    Claude response into the final message text.
  - Both fail safe to a still-on-brief, Python-templated fallback if the
    Claude call errors -- consistent with every other engine module's
    try/except pattern (prompt_builder, concept_proposal, etc.).
  - Wiring: onboarding.py sends the reflect_understanding() message right
    when onboarding completes (state -> done), before the existing
    credits/help message.
  - Wiring: orchestrator.py sends the reflect_first_result() message
    ONLY for a business's very first-ever generation (last_generation_id
    is None) -- NOT for a second/later generation, which still gets the
    plain "Creating your design..." status line.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_brand_reflection.db"
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

from app.engine import brand_reflection  # noqa: E402
from app.engine.context import BusinessContext  # noqa: E402


class _FakeResponse:
    def __init__(self, text):
        self.content = [type("Block", (), {"text": text})()]


def _ctx(**overrides):
    base = dict(name="Copper & Crumb", industry="bakery", tone="premium", language="en")
    base.update(overrides)
    return BusinessContext(**base)


async def part1_direct_unit_tests():
    print("=" * 60)
    print("TEST 1: reflect_understanding() parses a well-formed response")
    print("=" * 60)

    async def fake_create_message(**kwargs):
        return _FakeResponse('{"message": "Got it.\\nYou run a handmade gifting business.\\nI\'m going to remember that your brand needs to feel warm, personal — not like a mass-produced catalogue."}')

    brand_reflection.create_message = fake_create_message
    result = await brand_reflection.reflect_understanding(_ctx())
    assert result.startswith("Got it.\n"), f"FAIL: expected the message verbatim, got {result!r}"
    assert "handmade gifting business" in result
    print(f"PASS: {result!r}\n")

    print("=" * 60)
    print("TEST 2: reflect_understanding() falls back safely on a Claude failure")
    print("=" * 60)

    async def fake_create_message_fails(**kwargs):
        raise RuntimeError("simulated API failure")

    brand_reflection.create_message = fake_create_message_fails
    result = await brand_reflection.reflect_understanding(_ctx(industry="salon", tone="bold"))
    assert result.startswith("Got it.\n"), f"FAIL: expected the fallback to still open with 'Got it.', got {result!r}"
    assert "salon" in result, f"FAIL: expected the fallback to use the real industry, got {result!r}"
    assert "mass-produced catalogue" in result, f"FAIL: expected the fixed closing phrase, got {result!r}"
    print(f"PASS (fallback): {result!r}\n")

    print("=" * 60)
    print("TEST 3: reflect_first_result() parses a well-formed response")
    print("=" * 60)

    async def fake_create_message_2(**kwargs):
        return _FakeResponse(
            '{"message": "I\'ve got a pretty good idea of your brand now.\\n'
            'There\'s one thing I think we can improve:\\n'
            'Your current posts lean generic rather than premium and handcrafted.\\n'
            'So I want to try something different.\\nGive me a moment."}'
        )

    brand_reflection.create_message = fake_create_message_2
    result = await brand_reflection.reflect_first_result(_ctx())
    assert result.startswith("I've got a pretty good idea of your brand now.\n"), f"FAIL: got {result!r}"
    assert result.endswith("Give me a moment."), f"FAIL: got {result!r}"
    print(f"PASS: {result!r}\n")

    print("=" * 60)
    print("TEST 4: reflect_first_result() falls back safely on a Claude failure")
    print("=" * 60)
    brand_reflection.create_message = fake_create_message_fails
    result = await brand_reflection.reflect_first_result(_ctx(tone="friendly"))
    assert result.startswith("I've got a pretty good idea of your brand now.\n"), f"FAIL: got {result!r}"
    assert result.endswith("Give me a moment."), f"FAIL: got {result!r}"
    assert "friendly" in result, f"FAIL: expected the fallback to reference the real tone, got {result!r}"
    print(f"PASS (fallback): {result!r}\n")


async def part2_onboarding_wiring():
    print("=" * 60)
    print("TEST 5: onboarding.py sends reflect_understanding() right when onboarding completes")
    print("=" * 60)
    from app import onboarding
    from app.db import get_session
    from app.models import Business
    from app.schemas import IncomingMessage

    sent = []

    async def fake_send_text(to, body):
        sent.append(body)

    async def fake_send_buttons(to, body, buttons):
        sent.append(body)

    onboarding.send_text = fake_send_text
    onboarding.send_buttons = fake_send_buttons

    async def fake_detect_language(text):
        return "en"

    async def fake_t(key, language, english_text, **kwargs):
        return english_text.format(**kwargs) if kwargs else english_text

    onboarding.i18n.detect_language = fake_detect_language
    onboarding.i18n.t = fake_t

    async def fake_classify(user_message):
        return {"intent": "OTHER", "brief": user_message}

    onboarding.intent_engine.classify = fake_classify

    reflect_calls = []

    async def fake_reflect_understanding(ctx):
        reflect_calls.append(ctx.industry)
        return "Got it.\nYou run a restaurant.\nI'm going to remember that your brand needs to feel warm and inviting — not like a mass-produced catalogue."

    brand_reflection.reflect_understanding = fake_reflect_understanding

    phone = "919999999970"
    with get_session() as db:
        biz = Business(phone=phone, onboarding_state="new")
        db.add(biz)
        db.flush()
        biz_id = biz.id

    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="hi"))
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="Test Restaurant"))
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", button_id="restaurant", text="Restaurant"))
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="skip"))
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="skip"))
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="skip"))
    sent.clear()
    reflect_calls.clear()
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", button_id="friendly", text="Friendly"))

    assert reflect_calls == ["restaurant"], f"FAIL: expected reflect_understanding() called once with the real industry, got {reflect_calls}"
    assert len(sent) >= 2, f"FAIL: expected the reflection message + the credits/help message, got {sent}"
    assert sent[0].startswith("Got it.\n"), f"FAIL: expected the reflection message sent first, got {sent[0]!r}"
    print(f"PASS: reflection sent first ({sent[0]!r}), then the existing done message ({sent[1][:40]!r}...)\n")


async def part3_orchestrator_wiring():
    print("=" * 60)
    print("TEST 6: orchestrator.py sends reflect_first_result() ONLY for the very first generation")
    print("=" * 60)
    import io
    from PIL import Image
    from app.whatsapp import client as wa_client
    from app.engine import orchestrator
    from app.db import get_session
    from app.models import Business, BrandProfile
    from app.schemas import IncomingMessage
    from app.engine import intent as intent_engine, concept_proposal, prompt_builder, image_gen, quality, caption as caption_engine

    def png_bytes():
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), color=(10, 20, 30)).save(buf, format="PNG")
        return buf.getvalue()

    sent_texts = []

    async def fake_send_text(to, body):
        sent_texts.append(body)

    async def fake_send_image(to, image_url, caption=""):
        pass

    wa_client.send_text = fake_send_text
    wa_client.send_image = fake_send_image
    orchestrator.send_text = fake_send_text
    orchestrator.send_image = fake_send_image

    async def fake_classify_intent(user_message):
        return {"intent": "GENERATE", "brief": user_message}

    intent_engine.classify = fake_classify_intent
    orchestrator.intent_engine.classify = fake_classify_intent

    async def fake_decide(ctx, user_message):
        return {"decision": "SPECIFIC_ENOUGH", "brief": user_message}

    concept_proposal.decide = fake_decide
    orchestrator.concept_proposal.decide = fake_decide

    async def fake_build(ctx, user_brief):
        return {"image_prompt": "a creative", "headline_text": "Sale", "notes_for_caption": user_brief}

    prompt_builder.build = fake_build
    orchestrator.prompt_builder.build = fake_build

    async def fake_generate_images(prompt, count=2, reference_image=None):
        return [png_bytes(), png_bytes()]

    image_gen.generate_images = fake_generate_images
    orchestrator.image_gen.generate_images = fake_generate_images

    async def fake_score_and_pick(images):
        return {"best_index": 0, "best_score": 90, "issues": []}

    quality.score_and_pick = fake_score_and_pick
    orchestrator.quality.score_and_pick = fake_score_and_pick

    async def fake_caption_generate(ctx, notes_for_caption):
        return {"caption": "Great!", "hashtags": "#offer"}

    caption_engine.generate = fake_caption_generate
    orchestrator.caption_engine.generate = fake_caption_generate

    def fake_upload_creative(business_id, generation_id, image_bytes):
        return f"https://fake.example.com/{generation_id}.png"

    def fake_upload_base_image(business_id, generation_id, image_bytes):
        return f"https://fake.example.com/{generation_id}_base.png"

    orchestrator.upload_creative = fake_upload_creative
    orchestrator.upload_base_image = fake_upload_base_image

    first_result_calls = []

    async def fake_reflect_first_result(ctx):
        first_result_calls.append(ctx.industry)
        return "I've got a pretty good idea of your brand now.\nThere's one thing I think we can improve:\n(mocked)\nSo I want to try something different.\nGive me a moment."

    brand_reflection.reflect_first_result = fake_reflect_first_result
    orchestrator.brand_reflection.reflect_first_result = fake_reflect_first_result

    phone = "919999999971"
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="bakery", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        db.add(BrandProfile(business_id=biz_id, tone="premium"))

    # First-ever generation -> reflect_first_result() should fire, its
    # message sent instead of the plain "Creating your design..." line.
    sent_texts.clear()
    first_result_calls.clear()
    await orchestrator.generate(biz_id, IncomingMessage(sender=phone, type="text", text="Create a weekend offer post"))

    assert first_result_calls == ["bakery"], f"FAIL: expected reflect_first_result() called once on the first generation, got {first_result_calls}"
    assert any("pretty good idea of your brand" in t for t in sent_texts), f"FAIL: expected the reflection message sent, got {sent_texts}"
    assert not any("Creating your design" in t for t in sent_texts), f"FAIL: the plain status line should NOT be sent on the first generation, got {sent_texts}"
    print(f"PASS (1st generation): reflect_first_result() fired, sent: {[t[:40] for t in sent_texts]}\n")

    # Second generation for the SAME business -> plain status line, no
    # reflect_first_result() call this time.
    sent_texts.clear()
    first_result_calls.clear()
    await orchestrator.generate(biz_id, IncomingMessage(sender=phone, type="text", text="Create a Diwali post"))

    assert first_result_calls == [], f"FAIL: reflect_first_result() should NOT fire on a later generation, got {first_result_calls}"
    assert any("Creating your design" in t for t in sent_texts), f"FAIL: expected the plain status line on a later generation, got {sent_texts}"
    print(f"PASS (2nd generation): plain status line used instead, sent: {[t[:40] for t in sent_texts]}\n")

    print("ALL TESTS PASSED")


async def run():
    await part1_direct_unit_tests()
    await part2_onboarding_wiring()
    await part3_orchestrator_wiring()


asyncio.run(run())
