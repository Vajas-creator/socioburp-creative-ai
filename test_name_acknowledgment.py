"""
Regression test for a small UX gap reported alongside the carousel-typo
bug: a client volunteering their name mid-conversation ("by the way, my
name's Priya") got the generic OTHER-intent menu reply ("I'm Sakshi, your
creative partner here! Try something like...") right back -- as if they
hadn't said anything. Fixed with persona.extract_stated_name(), checked in
orchestrator.generate()'s QUESTION/OTHER branch before falling back to
that generic reply.

Covers:
  - persona.extract_stated_name() recognizes common explicit phrasings
    ("my name is X", "my name's X", "call me X", "you can call me X") and
    capitalizes the extracted name; returns None for unrelated OTHER-ish
    text so the old generic reply is untouched for genuinely unclear
    messages.
  - orchestrator.generate() integration: a stated name updates
    Business.owner_name and replies with a warm, direct acknowledgment
    instead of the generic menu -- and does NOT run the paid generation
    pipeline (no credit charged for a conversational aside).
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_name_acknowledgment.db"
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

from app import persona  # noqa: E402


def test_extract_stated_name():
    print("=" * 60)
    print("TEST 1: extract_stated_name() -- positive phrasings")
    print("=" * 60)
    cases = [
        ("by the way, my name is Priya", "Priya"),
        ("my name's Rohan", "Rohan"),
        ("you can call me Asha", "Asha"),
        ("just call me Dev, everyone does", "Dev"),
        ("My Name Is SUNITA", "Sunita"),
    ]
    for text, expected in cases:
        got = persona.extract_stated_name(text)
        assert got == expected, f"FAIL: {text!r} -> expected {expected!r}, got {got!r}"
    print(f"PASS: all {len(cases)} phrasings correctly extracted\n")

    print("=" * 60)
    print("TEST 2: extract_stated_name() -- unrelated text returns None")
    print("=" * 60)
    negative_cases = [
        "create a weekend offer post", "make it more premium", "hi",
        "what's the price", "call me back later today",
    ]
    for text in negative_cases:
        got = persona.extract_stated_name(text)
        assert got is None, f"FAIL: expected {text!r} to return None, got {got!r}"
    print(f"PASS: all {len(negative_cases)} unrelated phrasings correctly returned None\n")


async def test_orchestrator_integration():
    print("=" * 60)
    print("TEST 3: orchestrator.generate() -- a stated name is acknowledged, not menu'd")
    print("=" * 60)

    from app.whatsapp import client as wa_client
    sent = []

    async def fake_send_text(to, body):
        sent.append(body)

    wa_client.send_text = fake_send_text

    import app.engine.orchestrator as orch
    orch.send_text = fake_send_text

    async def fake_classify(text):
        return {"intent": "OTHER", "brief": text}

    orch.intent_engine.classify = fake_classify

    async def fake_classify_scope(text):
        return "UNCLEAR"

    orch.marketing_advisor.classify_scope = fake_classify_scope

    from app.db import get_session
    from app.models import Business
    from app.schemas import IncomingMessage
    from app.credits import add_credits, get_balance

    phone = "919999999980"
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="salon", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        add_credits(db, biz_id, 20, reason="signup_bonus")

    balance_before = get_balance(biz_id)

    await orch.generate(biz_id, IncomingMessage(sender=phone, type="text", text="oh by the way, my name is Priya"))

    assert len(sent) == 1, f"FAIL: expected exactly 1 message, got {sent}"
    assert "Priya" in sent[0], f"FAIL: expected the name acknowledged in the reply, got {sent[0]!r}"
    assert "Sakshi, your creative partner" not in sent[0], f"FAIL: expected the acknowledgment, not the generic menu, got {sent[0]!r}"

    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id).first()
        assert biz.owner_name == "Priya", f"FAIL: expected owner_name to be saved, got {biz.owner_name!r}"

    assert get_balance(biz_id) == balance_before, "FAIL: a conversational aside should not charge a credit"
    print(f"PASS: name acknowledged and saved, no credit charged: {sent[0]!r}\n")

    print("=" * 60)
    print("TEST 4: orchestrator.generate() -- genuinely unclear OTHER text still gets the generic menu")
    print("=" * 60)
    sent.clear()
    await orch.generate(biz_id, IncomingMessage(sender=phone, type="text", text="hmm not sure"))
    assert len(sent) == 1 and "Sakshi, your creative partner" in sent[0], (
        f"FAIL: expected the generic menu reply for unrelated OTHER text, got {sent}"
    )
    print("PASS: generic menu still fires for genuinely unrelated OTHER text\n")

    print("ALL TESTS PASSED")


test_extract_stated_name()
asyncio.run(test_orchestrator_integration())
