"""
Test for Sakshi as a marketing consultant (Priority 5 of the Aug 2026
consolidated fix list): marketing/growth questions ("what should I
charge", "when should I post this") get answered directly instead of the
generic "try something like..." menu, genuinely off-topic messages get
redirected back to Sakshi's actual job, and casual/ambiguous messages
still fall through to the old generic menu unchanged.

Covers:
  - marketing_advisor.classify_scope() fails safe to UNCLEAR on a
    classifier error (never blocks a reply).
  - marketing_advisor.answer() extracts only text blocks from the
    response (ignores server_tool_use/web_search_tool_result blocks),
    and fails safe to an apology string on error rather than raising.
  - orchestrator.generate() integration: a MARKETING-scoped message gets
    marketing_advisor's answer, not the generic menu; an OFF_TOPIC one
    gets the redirect; an UNCLEAR one still gets the generic menu
    (existing behavior, unchanged) -- and none of these charge a credit.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_marketing_advisor.db"
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

from app.engine import marketing_advisor  # noqa: E402
from app.engine.context import BusinessContext  # noqa: E402


class FakeContent:
    def __init__(self, type_, text=None):
        self.type = type_
        self.text = text


class FakeResponse:
    def __init__(self, content):
        self.content = content


async def test_classify_scope_fails_safe():
    print("=" * 60)
    print("TEST 1: classify_scope() fails safe to UNCLEAR on a classifier error")
    print("=" * 60)

    async def fake_create_message_error(**kwargs):
        raise RuntimeError("simulated API failure")

    marketing_advisor.create_message = fake_create_message_error
    result = await marketing_advisor.classify_scope("what should I charge for my cakes?")
    assert result == "UNCLEAR", f"FAIL: expected UNCLEAR on error, got {result}"
    print("PASS: classifier failure degrades to UNCLEAR, not a crash\n")


async def test_answer_extracts_text_only():
    print("=" * 60)
    print("TEST 2: answer() extracts only text blocks, ignoring tool-use/tool-result blocks")
    print("=" * 60)

    async def fake_create_message(**kwargs):
        return FakeResponse([
            FakeContent("server_tool_use", text=None),
            FakeContent("text", text="Charge 15-20% above your nearest competitor for custom cakes."),
            FakeContent("web_search_tool_result", text=None),
        ])

    marketing_advisor.create_message = fake_create_message
    ctx = BusinessContext(name="Sweet Treats", industry="bakery")
    result = await marketing_advisor.answer(ctx, "what should I charge?")
    assert result == "Charge 15-20% above your nearest competitor for custom cakes.", (
        f"FAIL: expected only the text block content, got {result!r}"
    )
    print(f"PASS: {result!r}\n")

    print("=" * 60)
    print("TEST 3: answer() fails safe to an apology string, not a raised exception")
    print("=" * 60)

    async def fake_create_message_error(**kwargs):
        raise RuntimeError("simulated API failure")

    marketing_advisor.create_message = fake_create_message_error
    result = await marketing_advisor.answer(ctx, "what should I charge?")
    assert "snag" in result.lower() or "sorry" in result.lower(), f"FAIL: expected a fail-safe apology, got {result!r}"
    print(f"PASS: {result!r}\n")


async def test_orchestrator_integration():
    print("=" * 60)
    print("TEST 4: orchestrator.generate() -- MARKETING scope gets a real answer, not the generic menu")
    print("=" * 60)

    from app.whatsapp import client as wa_client
    sent = []

    async def fake_send_text(to, body):
        sent.append(body)

    wa_client.send_text = fake_send_text

    import app.engine.orchestrator as orch
    orch.send_text = fake_send_text

    async def fake_intent_classify(text):
        return {"intent": "OTHER", "brief": text}

    orch.intent_engine.classify = fake_intent_classify

    async def fake_classify_scope(text):
        if "competitor" in text.lower():
            return "MARKETING"
        if "weather" in text.lower():
            return "OFF_TOPIC"
        return "UNCLEAR"

    async def fake_answer(ctx, text):
        return "Price 15-20% above your nearest competitor, and lead with what makes your cakes different."

    orch.marketing_advisor.classify_scope = fake_classify_scope
    orch.marketing_advisor.answer = fake_answer

    from app.db import get_session
    from app.models import Business
    from app.schemas import IncomingMessage
    from app.credits import add_credits, get_balance

    phone = "919999999990"
    with get_session() as db:
        biz = Business(phone=phone, name="Sweet Treats", industry="bakery", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        add_credits(db, biz_id, 20, reason="signup_bonus")

    balance_before = get_balance(biz_id)

    await orch.generate(biz_id, IncomingMessage(sender=phone, type="text", text="what should I charge compared to competitors?"))
    assert len(sent) == 1, f"FAIL: expected exactly 1 message, got {sent}"
    assert "competitor" in sent[0].lower(), f"FAIL: expected the marketing answer, got {sent[0]!r}"
    assert "Sakshi, your creative partner" not in sent[0], f"FAIL: expected the real answer, not the generic menu, got {sent[0]!r}"
    assert get_balance(biz_id) == balance_before, "FAIL: answering a marketing question should not charge a credit"
    print(f"PASS: {sent[0]!r}\n")

    print("=" * 60)
    print("TEST 5: orchestrator.generate() -- OFF_TOPIC scope gets redirected, not answered")
    print("=" * 60)
    sent.clear()
    await orch.generate(biz_id, IncomingMessage(sender=phone, type="text", text="what's the weather like today?"))
    assert len(sent) == 1, f"FAIL: expected exactly 1 message, got {sent}"
    assert "Sweet Treats" in sent[0], f"FAIL: expected a personalized redirect, got {sent[0]!r}"
    assert "outside what I do" in sent[0], f"FAIL: expected the off-topic redirect, got {sent[0]!r}"
    print(f"PASS: {sent[0]!r}\n")

    print("=" * 60)
    print("TEST 6: orchestrator.generate() -- UNCLEAR scope still gets the generic menu (unchanged)")
    print("=" * 60)
    sent.clear()
    await orch.generate(biz_id, IncomingMessage(sender=phone, type="text", text="hmm not sure"))
    assert len(sent) == 1 and "Sakshi, your creative partner" in sent[0], (
        f"FAIL: expected the generic menu for UNCLEAR scope, got {sent}"
    )
    print("PASS: generic menu still fires for genuinely unclear messages\n")

    print("ALL TESTS PASSED")


async def run():
    await test_classify_scope_fails_safe()
    await test_answer_extracts_text_only()
    await test_orchestrator_integration()


asyncio.run(run())
