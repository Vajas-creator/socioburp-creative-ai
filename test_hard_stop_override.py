"""
Test for the Aug 2026 "hard-stop keyword override" fix (Priority 3 of the
live-test follow-up list).

Working definition confirmed with the product owner: if a message contains
a clear, unambiguous generation command -- "create an image," "make me a
post," "generate a carousel" -- treat that as an immediate intent to
generate, and skip past any pending clarifying-question state instead of
swallowing it as an answer to a question the client has effectively
bypassed. Does NOT override the approval gate -- only skips upstream
clarifying questions (e.g. "how many slides?").

Confirmed NOT already solved by the earlier LLM-based intent
classification rebuild: router_intent.py's INTENTS enum had no concept of
"a new, self-contained generation command" distinct from an answer to
whatever's pending, and both carousel.advance() and image_intent.advance()
explicitly assumed (per their own docstrings) that anything reaching them
was meant as an answer -- confirmed via code trace, not guessed.

Fix: a new NEW_GENERATION_REQUEST intent (both the LLM classifier and its
keyword-fallback safety net), checked deliberately AFTER carousel
detection (so "generate a carousel of X" still routes as CAROUSEL_REQUEST,
unchanged) -- app/router.py now treats it as a topic switch during a
pending carousel/image-intent negotiation, same as GLOBAL_COMMAND/
LOGO_UPLOAD already were, dropping the pending state and falling through
to normal handling (which ultimately reaches orchestrator.generate() same
as any OTHER-classified message would).

Covers:
  - _fallback_classify(): recognizes explicit imperative generation
    commands ("create a...", "generate a...", "make me a...", "build me
    a...", "design a...") as NEW_GENERATION_REQUEST; does NOT misfire on
    a bare content description/answer with no imperative framing; a
    carousel mention still wins over the generic imperative check.
  - router.py: a NEW_GENERATION_REQUEST message mid-pending_carousel
    negotiation drops the carousel state and reaches generate() instead
    of being fed to carousel.advance() as a bogus "answer".
  - router.py: same for mid-pending_image_intent.
  - router.py: an ordinary answer to a pending question (no imperative
    generation verb) is completely unaffected -- still routes to
    carousel.advance()/image_intent.advance() as before.
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_hard_stop_override.db"
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

from app.engine import router_intent  # noqa: E402
from app.whatsapp import client as wa_client  # noqa: E402
from app import router  # noqa: E402


def test_fallback_classify_recognizes_new_generation_request():
    print("=" * 60)
    print("TEST 1: explicit imperative generation commands classify as NEW_GENERATION_REQUEST")
    print("=" * 60)
    cases = [
        "create a Diwali poster for my sweets",
        "generate an image for my new menu",
        "make me a post about the weekend sale",
        "make a flyer for the sale",
        "build me a flyer for the sale",
        "design a poster for diwali",
        "Design me something festive",
    ]
    for text in cases:
        result = router_intent._fallback_classify(text)
        assert result["intent"] == "NEW_GENERATION_REQUEST", f"FAIL: {text!r} -> {result}"
    print(f"PASS: all {len(cases)} imperative phrasings recognized\n")

    print("=" * 60)
    print("TEST 2: a bare content description/answer does NOT misfire as NEW_GENERATION_REQUEST")
    print("=" * 60)
    cases_other = [
        "3 product shots and a lifestyle shot",
        "a Diwali poster with diyas and rangoli",
        "product shot, behind-the-scenes, pricing shot",
        "warm gold tones please",
    ]
    for text in cases_other:
        result = router_intent._fallback_classify(text)
        assert result["intent"] != "NEW_GENERATION_REQUEST", f"FAIL: {text!r} incorrectly classified as NEW_GENERATION_REQUEST"
    print(f"PASS: all {len(cases_other)} plain answers/descriptions correctly NOT flagged\n")

    print("=" * 60)
    print("TEST 3: a carousel mention still wins over the generic imperative check")
    print("=" * 60)
    result = router_intent._fallback_classify("generate a carousel of my new products")
    assert result["intent"] == "CAROUSEL_REQUEST", f"FAIL: expected CAROUSEL_REQUEST to take priority, got {result}"
    print(f"PASS: {result}\n")

    print("=" * 60)
    print("TEST 4: NEW_GENERATION_REQUEST is a real member of INTENTS")
    print("=" * 60)
    assert "NEW_GENERATION_REQUEST" in router_intent.INTENTS
    print("PASS\n")


sent_texts = []


async def fake_send_text(to, body):
    sent_texts.append(body)


wa_client.send_text = fake_send_text
router.send_text = fake_send_text


class _FakeContent:
    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeContent(text)]


async def test_carousel_negotiation_bypassed_by_new_generation_request():
    from app.db import get_session
    from app.models import Business, ConversationState
    from app.schemas import IncomingMessage
    from app.engine import carousel as carousel_mod

    print("=" * 60)
    print("TEST 5: a NEW_GENERATION_REQUEST mid-carousel-negotiation drops the negotiation and reaches generate()")
    print("=" * 60)

    # Exercises the REAL LLM classification path (mocked), not the
    # keyword fallback -- this natural, mid-sentence-pivot phrasing
    # ("actually, X instead") is exactly what an LLM classifier can
    # recognize by meaning but a narrow anchored-regex fallback can't,
    # and is deliberately not required to (see this module's docstring:
    # the fallback is a safety net for an outage, not the primary path).
    async def fake_create_message(**kwargs):
        return _FakeResponse('{"intent": "NEW_GENERATION_REQUEST", "command": null}')

    router_intent.create_message = fake_create_message

    carousel_advance_calls = []

    async def fake_carousel_advance(business_id, msg, pending):
        carousel_advance_calls.append(msg.text)

    carousel_mod.advance = fake_carousel_advance

    generate_calls = []

    async def fake_generate(business_id, msg):
        generate_calls.append(msg.text)

    from app.engine import orchestrator as orch
    orch.generate = fake_generate

    with get_session() as db:
        biz = Business(phone="919999998820", name="Test Biz", industry="bakery", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        db.add(ConversationState(business_id=biz_id, pending_carousel='{"stage": "awaiting_count"}'))

    from app.credits import add_credits
    with get_session() as db:
        add_credits(db, biz_id, 20, reason="signup_bonus")

    msg = IncomingMessage(sender="919999998820", type="text", text="actually, create a single poster for my new dish instead")
    await router._process_message(biz_id, msg)

    assert carousel_advance_calls == [], f"FAIL: expected carousel.advance() NOT to run, got {carousel_advance_calls}"
    assert generate_calls == [msg.text], f"FAIL: expected generate() to run with the new request, got {generate_calls}"
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == biz_id).first()
        assert convo.pending_carousel is None, "FAIL: expected the carousel negotiation to be dropped"
    print("PASS: carousel negotiation correctly bypassed, message reached generate()\n")

    print("=" * 60)
    print("TEST 6: an ORDINARY answer mid-carousel-negotiation is completely unaffected")
    print("=" * 60)
    carousel_advance_calls.clear()
    generate_calls.clear()

    async def fake_create_message_other(**kwargs):
        return _FakeResponse('{"intent": "OTHER", "command": null}')

    router_intent.create_message = fake_create_message_other

    with get_session() as db:
        biz2 = Business(phone="919999998821", name="Test Biz 2", industry="bakery", onboarding_state="done")
        db.add(biz2)
        db.flush()
        biz2_id = biz2.id
        db.add(ConversationState(business_id=biz2_id, pending_carousel='{"stage": "awaiting_count"}'))
        add_credits(db, biz2_id, 20, reason="signup_bonus")

    msg2 = IncomingMessage(sender="919999998821", type="text", text="3")
    await router._process_message(biz2_id, msg2)

    assert carousel_advance_calls == ["3"], f"FAIL: expected the plain answer to still reach carousel.advance(), got {carousel_advance_calls}"
    assert generate_calls == [], "FAIL: an ordinary answer must not be misrouted to generate()"
    print("PASS: an ordinary answer is unaffected, still routes to carousel.advance()\n")


async def test_image_intent_negotiation_bypassed_by_new_generation_request():
    from app.db import get_session
    from app.models import Business, ConversationState
    from app.schemas import IncomingMessage
    from app.engine import image_intent as ii_mod
    from app.credits import add_credits

    print("=" * 60)
    print("TEST 7: a NEW_GENERATION_REQUEST mid-image-intent-negotiation drops the negotiation and reaches generate()")
    print("=" * 60)

    async def fake_create_message(**kwargs):
        return _FakeResponse('{"intent": "NEW_GENERATION_REQUEST", "command": null}')

    router_intent.create_message = fake_create_message

    image_intent_advance_calls = []

    async def fake_image_intent_advance(business_id, msg, pending):
        image_intent_advance_calls.append(msg.text)

    ii_mod.advance = fake_image_intent_advance

    generate_calls = []

    async def fake_generate(business_id, msg):
        generate_calls.append(msg.text)

    from app.engine import orchestrator as orch
    orch.generate = fake_generate

    with get_session() as db:
        biz = Business(phone="919999998822", name="Test Biz 3", industry="bakery", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        db.add(ConversationState(business_id=biz_id, pending_image_intent='{"reference_image_url": "https://fake.example.com/x.png"}'))
        add_credits(db, biz_id, 20, reason="signup_bonus")

    msg = IncomingMessage(sender="919999998822", type="text", text="never mind that, generate a fresh post about our new menu")
    await router._process_message(biz_id, msg)

    assert image_intent_advance_calls == [], f"FAIL: expected image_intent.advance() NOT to run, got {image_intent_advance_calls}"
    assert generate_calls == [msg.text], f"FAIL: expected generate() to run, got {generate_calls}"
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == biz_id).first()
        assert convo.pending_image_intent is None
    print("PASS: image-intent negotiation correctly bypassed, message reached generate()\n")


async def run():
    test_fallback_classify_recognizes_new_generation_request()
    await test_carousel_negotiation_bypassed_by_new_generation_request()
    await test_image_intent_negotiation_bypassed_by_new_generation_request()
    print("ALL TESTS PASSED")


asyncio.run(run())
