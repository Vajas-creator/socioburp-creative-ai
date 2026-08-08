"""
Test for the Maya persona's identity-disclosure handling (app/persona.py +
the router.py hook).

Proves: common phrasings of "are you real/AI/a bot" are caught BEFORE
falling through to the normal AI intent classifier, and get an honest,
in-character disclosure response — not the generic "I'm your creative
partner, try..." fallback a QUESTION/OTHER classification would produce.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_persona.db"
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


def test_pattern_matching():
    print("=" * 60)
    print("TEST 1: is_identity_question() pattern matching")
    print("=" * 60)

    positive_cases = [
        "are you real?", "Are you a bot", "are you a real person",
        "ARE YOU HUMAN", "are you AI", "is this a bot?", "who are you",
        "what are you", "hey are you a bot or a real person lol",
    ]
    for text in positive_cases:
        assert persona.is_identity_question(text), f"FAIL: expected {text!r} to match an identity question"
    print(f"PASS: all {len(positive_cases)} positive phrasings correctly matched\n")

    negative_cases = [
        "create a weekend offer post", "make it more premium",
        "what's the price of your credits", "hi", "topup",
        "are you able to make this brighter",  # contains "are you" but isn't an identity question
    ]
    for text in negative_cases:
        assert not persona.is_identity_question(text), f"FAIL: expected {text!r} to NOT match"
    print(f"PASS: all {len(negative_cases)} unrelated phrasings correctly did NOT match\n")


async def test_router_integration():
    print("=" * 60)
    print("TEST 2: router.py integration — disclosure fires before intent classification")
    print("=" * 60)

    from app.whatsapp import client as wa_client
    sent = []

    async def fake_send_text(to, body):
        sent.append(body)

    wa_client.send_text = fake_send_text

    from app import router
    router.send_text = fake_send_text

    intent_classify_called = {"n": 0}

    async def fake_generate(business_id, msg):
        intent_classify_called["n"] += 1  # would only be reached if disclosure did NOT intercept

    import app.engine.orchestrator as orch
    orch.generate = fake_generate

    from app.db import get_session
    from app.models import Business
    from app.schemas import IncomingMessage

    phone = "919999999986"
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="salon", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id

    from app.credits import add_credits
    with get_session() as db:
        add_credits(db, biz_id, 20, reason="signup_bonus")

    await router._process_message(biz_id, IncomingMessage(sender=phone, type="text", text="are you a real person?"))

    assert len(sent) == 1, f"FAIL: expected exactly 1 message sent, got {sent}"
    assert "Maya" in sent[0], f"FAIL: expected the disclosure response naming Maya, got {sent[0]!r}"
    assert "AI" in sent[0], f"FAIL: expected honest AI disclosure in the response, got {sent[0]!r}"
    assert intent_classify_called["n"] == 0, "FAIL: the identity question should be intercepted BEFORE reaching generate()/intent classification"
    print(f"PASS: identity question intercepted before generate() ran, honest disclosure sent: {sent[0][:80]}...\n")

    print("=" * 60)
    print("TEST 3: a normal creative request is NOT intercepted, reaches generate() as usual")
    print("=" * 60)
    sent.clear()
    intent_classify_called["n"] = 0
    await router._process_message(biz_id, IncomingMessage(sender=phone, type="text", text="create a weekend offer post"))
    assert len(sent) == 0, f"FAIL: expected no direct send_text from the router for a normal request, got {sent}"
    assert intent_classify_called["n"] == 1, f"FAIL: expected generate() to be reached for a normal request, got {intent_classify_called['n']} calls"
    print("PASS: normal creative request correctly bypassed the identity check and reached generate()\n")

    print("ALL TESTS PASSED")


test_pattern_matching()
asyncio.run(test_router_integration())
