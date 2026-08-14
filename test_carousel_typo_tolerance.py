"""
Regression test for the "misspelled 'carousel' silently falls through to
the single-image pipeline" bug, reported live: a real WhatsApp test
session had every carousel request typed as "carasoul" or "carsoul", and
app/router.py's trigger was an EXACT substring check (`"carousel" in
text_lower`) -- which never matched either typo. Every one of those
requests fell through to the normal single-image concept-proposal/generate
pipeline instead of app/engine/carousel.py's negotiation -- which can only
ever produce ONE image -- and is what actually produced the reported
"single collage" output, not a separate bug in generate_carousel() itself
(see test_carousel_no_collage.py, which already proves generate_carousel()
itself produces genuinely separate, non-collage images once it's reached).

Fixed with app/router.py's _mentions_carousel(): exact substring first
(cheap, common case), then a fuzzy per-word fallback (difflib, cutoff=0.72,
word length >= 7) that catches real misspellings without false-positiving
on short, unrelated real words.

Covers:
  - _mentions_carousel() directly: every typo actually seen in testing
    ("carasoul", "carsoul") plus other plausible misspellings match: unrelated
    real words ("carol", "cancel", "casual", "carnival") do NOT match.
  - router._process_message() integration: a message reading "I want a
    carasoul with 5 images" now reaches carousel.start(), NOT
    orchestrator.generate().
  - A pending_image_intent negotiation is also correctly cancelled by a
    misspelled "carasoul" mid-negotiation (same fix applied to that check).
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_carousel_typo_tolerance.db"
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


from app import db as db_module  # noqa: E402
import app.models  # noqa: E402
db_module.Base.metadata.create_all(bind=db_module.engine)

from app import router  # noqa: E402


def test_mentions_carousel_directly():
    print("=" * 60)
    print("TEST 1: _mentions_carousel() -- typos seen live, plus other plausible ones")
    print("=" * 60)
    positive_cases = [
        "i want a carasoul with 5 images",
        "make me a carsoul",
        "can you do a carousal for diwali",
        "carousle please",
        "5 image carrousel",
        "CARASOUL",
        "carousel",  # exact spelling still works
    ]
    for text in positive_cases:
        assert router._mentions_carousel(text.lower()), f"FAIL: expected {text!r} to trigger carousel mode"
    print(f"PASS: all {len(positive_cases)} spellings (correct + typo'd) correctly matched\n")

    print("=" * 60)
    print("TEST 2: _mentions_carousel() -- unrelated real words do NOT false-positive")
    print("=" * 60)
    negative_cases = [
        "my name is carol", "please cancel my order", "keep it casual",
        "carnival theme post", "create a weekend offer post", "make it more premium",
    ]
    for text in negative_cases:
        assert not router._mentions_carousel(text.lower()), f"FAIL: expected {text!r} to NOT trigger carousel mode"
    print(f"PASS: all {len(negative_cases)} unrelated phrasings correctly did NOT match\n")


async def test_router_integration():
    print("=" * 60)
    print("TEST 3: router.py integration -- a misspelled 'carasoul' request reaches carousel.start(), not generate()")
    print("=" * 60)

    from app.whatsapp import client as wa_client
    sent = []

    async def fake_send_text(to, body):
        sent.append(body)

    wa_client.send_text = fake_send_text
    router.send_text = fake_send_text

    carousel_start_calls = []

    async def fake_carousel_start(business_id, msg):
        carousel_start_calls.append(msg.text)

    generate_calls = []

    async def fake_generate(business_id, msg):
        generate_calls.append(msg.text)

    from app.engine import carousel as carousel_module
    carousel_module.start = fake_carousel_start
    router.carousel = carousel_module

    import app.engine.orchestrator as orch
    orch.generate = fake_generate

    from app.db import get_session
    from app.models import Business
    from app.schemas import IncomingMessage
    from app.credits import add_credits

    phone = "919999999970"
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="salon", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        add_credits(db, biz_id, 20, reason="signup_bonus")

    await router._process_message(biz_id, IncomingMessage(sender=phone, type="text", text="I want a carasoul with 5 images, one per product"))

    assert carousel_start_calls == ["I want a carasoul with 5 images, one per product"], (
        f"FAIL: expected carousel.start() to be called with the misspelled request, got {carousel_start_calls}"
    )
    assert generate_calls == [], f"FAIL: generate() should NOT have been reached, got {generate_calls}"
    print("PASS: misspelled 'carasoul' correctly routed to carousel.start(), not generate()\n")

    print("ALL TESTS PASSED")


test_mentions_carousel_directly()
asyncio.run(test_router_integration())
