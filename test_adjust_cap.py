"""
Test for the ADJUST round cap in orchestrator.py's pending-proposal handling.

Design: 2 rounds of pre-generation back-and-forth negotiation are allowed.
On what would be the 3rd consecutive ADJUST, the orchestrator stops
proposing and generates immediately with whatever's been gathered so far,
telling the client they can revise the actual image instead.

Trace:
  Message 1 (vague) -> NEEDS_PROPOSAL, adjust_count=0, proposal sent
  Message 2 (feedback) -> ADJUST, adjust_count becomes 1, new proposal sent
  Message 3 (feedback) -> ADJUST, adjust_count becomes 2, new proposal sent
  Message 4 (feedback) -> ADJUST would make it 3 -> CAPPED: generate now
    using round 3's concept_brief, pending_proposal cleared, no more looping
"""
import sys
import asyncio
import os
import json

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_adjust_cap.db"
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

from app.whatsapp import client as wa_client  # noqa: E402

sent_texts = []


async def fake_send_text(to, body):
    sent_texts.append(body)
    print(f"[SEND TEXT]: {body}\n")


wa_client.send_text = fake_send_text

from app.engine import orchestrator  # noqa: E402

async def _fake_content_policy_check(text):
    return {"allowed": True, "reason": None}

orchestrator.content_policy.check = _fake_content_policy_check
orchestrator.send_text = fake_send_text

from app.engine import intent as intent_engine  # noqa: E402


async def fake_classify(user_message):
    return {"intent": "GENERATE", "brief": user_message}


intent_engine.classify = fake_classify
orchestrator.intent_engine.classify = fake_classify

from app.engine import concept_proposal  # noqa: E402


async def fake_decide(ctx, user_message):
    return {
        "decision": "NEEDS_PROPOSAL",
        "proposal_text": "How about a warm festive theme with your logo bottom-right?",
        "concept_brief": "round0: initial vague request",
    }


concept_proposal.decide = fake_decide
orchestrator.concept_proposal.decide = fake_decide

adjust_round_counter = {"n": 0}


async def fake_interpret_reply(ctx, previous_proposal, client_reply):
    adjust_round_counter["n"] += 1
    n = adjust_round_counter["n"]
    return {
        "classification": "ADJUST",
        "proposal_text": f"Round {n} revised proposal — how about this instead?",
        "concept_brief": f"round{n}: concept brief after adjustment {n}",
    }


concept_proposal.interpret_reply = fake_interpret_reply
orchestrator.concept_proposal.interpret_reply = fake_interpret_reply

pipeline_calls = []


async def fake_run_generation(business_id, phone, ctx, brief, user_message, last_generation_id, is_revision, trigger_source=None, reference_image=None):
    pipeline_calls.append(brief)
    print(f"[GENERATION PIPELINE CALLED] brief={brief!r}")


orchestrator._run_generation = fake_run_generation

from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile, ConversationState  # noqa: E402
from app.engine.orchestrator import generate  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402


def get_pending(business_id):
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        return json.loads(convo.pending_proposal) if convo and convo.pending_proposal else None


async def run():
    phone = "919999999992"
    with get_session() as db:
        biz = Business(phone=phone, name="Adjust Cap Test Biz", industry="restaurant", onboarding_state="done")
        db.add(biz)
        db.flush()
        business_id = biz.id
        db.add(BrandProfile(business_id=business_id, tone="bold"))

    print("=" * 60)
    print("Message 1: vague request -> NEEDS_PROPOSAL")
    print("=" * 60)
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="make me something nice"))
    pending = get_pending(business_id)
    assert pending is not None, "FAIL: expected a pending proposal after message 1"
    assert pending["adjust_count"] == 0, f"FAIL: expected adjust_count=0 on initial proposal, got {pending}"
    assert len(pipeline_calls) == 0, f"FAIL: pipeline should not have run yet, got {pipeline_calls}"
    print("PASS: pending proposal set, adjust_count=0\n")

    print("=" * 60)
    print("Message 2: feedback -> ADJUST round 1")
    print("=" * 60)
    sent_texts.clear()
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="make it more colorful"))
    pending = get_pending(business_id)
    assert pending is not None, "FAIL: expected pending proposal to still exist after round 1 ADJUST"
    assert pending["adjust_count"] == 1, f"FAIL: expected adjust_count=1, got {pending}"
    assert len(pipeline_calls) == 0, f"FAIL: pipeline should still not have run, got {pipeline_calls}"
    assert "Round 1" in sent_texts[0], f"FAIL: expected round 1's proposal text sent, got {sent_texts}"
    print("PASS: round 1 ADJUST — new proposal sent, adjust_count=1, pipeline still not run\n")

    print("=" * 60)
    print("Message 3: feedback -> ADJUST round 2")
    print("=" * 60)
    sent_texts.clear()
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="actually less text on it"))
    pending = get_pending(business_id)
    assert pending is not None, "FAIL: expected pending proposal to still exist after round 2 ADJUST"
    assert pending["adjust_count"] == 2, f"FAIL: expected adjust_count=2, got {pending}"
    assert len(pipeline_calls) == 0, f"FAIL: pipeline should still not have run, got {pipeline_calls}"
    print("PASS: round 2 ADJUST — new proposal sent, adjust_count=2, pipeline still not run\n")

    print("=" * 60)
    print("Message 4: feedback -> would be ADJUST round 3 -> CAPPED, must generate now")
    print("=" * 60)
    sent_texts.clear()
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="hmm one more tweak"))
    pending = get_pending(business_id)
    assert pending is None, f"FAIL: pending_proposal should be cleared once capped, got {pending}"
    assert len(pipeline_calls) == 1, f"FAIL: expected the pipeline to run exactly once after the cap, got {pipeline_calls}"
    assert pipeline_calls[0] == "round3: concept brief after adjustment 3", f"FAIL: expected round 3's concept_brief to be used, got {pipeline_calls[0]!r}"
    assert any("adjust anything directly on the image" in t for t in sent_texts), f"FAIL: expected the transition message, got {sent_texts}"
    print(f"PASS: capped correctly — generated with round 3's brief ({pipeline_calls[0]!r}), pending cleared, transition message sent\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
