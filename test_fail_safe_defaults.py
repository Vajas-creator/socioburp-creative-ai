"""
Smoke test for the fail-safe defaults in intent classification and concept
proposal handling. Simulates a real Claude API failure (timeout, rate limit,
malformed response) at each of the three points that used to silently fail
toward spending a client's credit, and asserts they now fail toward asking
again instead: no pipeline run, no charge, pending_proposal left correct.

Unlike test_concept_proposal.py, this does NOT mock our own classify()/
decide()/interpret_reply() wrapper functions directly — it breaks the
underlying Anthropic client those functions call, so the actual except
blocks being tested run for real.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_failsafe.db")
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ["ANTHROPIC_API_KEY"] = "fake"

from app import db as db_module
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

db_module.engine = create_engine("sqlite:///./test_failsafe.db")
db_module.SessionLocal = sessionmaker(bind=db_module.engine)

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


import app.models  # noqa: E402
db_module.Base.metadata.create_all(bind=db_module.engine)

from app.whatsapp import client as wa_client  # noqa: E402
sent = []

async def fake_send_text(to, body):
    sent.append(body)
    print(f"[SEND TEXT]: {body}\n")

wa_client.send_text = fake_send_text

from app.engine import orchestrator  # noqa: E402
orchestrator.send_text = fake_send_text

pipeline_runs = []

async def fake_run_generation(business_id, phone, ctx, brief, user_message, last_generation_id, is_revision, trigger_source=None):
    pipeline_runs.append(brief)
    print(f"[GENERATION PIPELINE] brief={brief!r} revision={is_revision}")

orchestrator._run_generation = fake_run_generation

# --- Break the underlying Anthropic client, not our own wrappers — this
# exercises the REAL except blocks in intent.py / concept_proposal.py. ---
from app.engine import intent as intent_engine  # noqa: E402
from app.engine import concept_proposal  # noqa: E402


class _BrokenClient:
    class messages:
        @staticmethod
        async def create(*args, **kwargs):
            raise RuntimeError("simulated API failure (timeout/rate-limit/malformed response)")


intent_engine.client = _BrokenClient()
concept_proposal.client = _BrokenClient()

from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile, ConversationState  # noqa: E402
from app.engine.orchestrator import generate  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402


async def run():
    phone = "919999999998"
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="restaurant", onboarding_state="done")
        db.add(biz)
        db.flush()
        business_id = biz.id
        db.add(BrandProfile(business_id=business_id, tone="premium"))

    print("=" * 60)
    print("TEST 1: intent.classify() fails -> must NOT generate, must send generic reply")
    print("=" * 60)
    sent.clear()
    pipeline_runs.clear()
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="Hi"))
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        assert len(pipeline_runs) == 0, f"FAIL: pipeline ran on a plain 'Hi' after a classify() failure! runs={pipeline_runs}"
        assert convo.pending_proposal is None, "FAIL: a pending proposal got set from a classify() failure!"
        assert len(sent) == 1, f"FAIL: expected exactly 1 message, got {sent}"
        assert "creative partner" in sent[0], f"FAIL: expected the generic OTHER reply, got: {sent[0]!r}"
    print("PASS: classify() failure -> OTHER -> generic reply, no charge, no pipeline run\n")

    print("=" * 60)
    print("TEST 2: concept_proposal.decide() fails -> must ask a clarifying question, not generate")
    print("=" * 60)
    # Un-break intent so this message correctly reaches GENERATE and falls
    # through to decide() (still broken) -- otherwise TEST 1's broken
    # classify() would swallow this before decide() is ever reached.
    async def working_classify(user_message):
        return {"intent": "GENERATE", "brief": user_message}
    intent_engine.classify = working_classify
    orchestrator.intent_engine.classify = working_classify

    sent.clear()
    pipeline_runs.clear()
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="make me something nice"))
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        assert len(pipeline_runs) == 0, f"FAIL: pipeline ran despite decide() failing! runs={pipeline_runs}"
        assert convo.pending_proposal is not None, "FAIL: decide() failure should still set a pending proposal (the fallback question)!"
        assert len(sent) == 1, f"FAIL: expected exactly 1 message, got {sent}"
        assert "didn't quite catch" in sent[0], f"FAIL: expected the fallback clarifying question, got: {sent[0]!r}"
    print("PASS: decide() failure -> fallback clarifying question sent, pending set, no charge, no pipeline run\n")

    print("=" * 60)
    print("TEST 3: concept_proposal.interpret_reply() fails -> must RETRY, leave pending_proposal untouched")
    print("=" * 60)
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        pending_before = convo.pending_proposal
    sent.clear()
    pipeline_runs.clear()
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="yes that's great"))
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        assert len(pipeline_runs) == 0, f"FAIL: pipeline ran despite interpret_reply() failing! runs={pipeline_runs}"
        assert convo.pending_proposal == pending_before, "FAIL: pending_proposal was modified even though interpret_reply() failed!"
        assert len(sent) == 1, f"FAIL: expected exactly 1 message, got {sent}"
        assert "didn't quite catch" in sent[0], f"FAIL: expected the RETRY message, got: {sent[0]!r}"
    print("PASS: interpret_reply() failure -> RETRY message sent, pending_proposal untouched, no charge, no pipeline run\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
