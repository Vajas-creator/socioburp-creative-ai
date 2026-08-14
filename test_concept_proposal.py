"""
Smoke test for the concept proposal step. All Claude calls mocked so this
tests the CONTROL FLOW (when do we propose vs generate vs re-propose vs
proceed), not real model output.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_concept.db")
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ["ANTHROPIC_API_KEY"] = "fake"

from app import db as db_module
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

db_module.engine = create_engine("sqlite:///./test_concept.db")
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

async def _fake_content_policy_check(text):
    return {"allowed": True, "reason": None}

orchestrator.content_policy.check = _fake_content_policy_check
orchestrator.send_text = fake_send_text

# --- Stub the production pipeline — this test is about the proposal step's
# control flow (propose vs adjust vs confirm vs skip), not the pipeline ---
pipeline_runs = []


async def fake_run_generation(business_id, phone, ctx, brief, user_message, last_generation_id, is_revision, trigger_source=None, reference_image=None):
    pipeline_runs.append(brief)
    print(f"[GENERATION PIPELINE] brief={brief!r} revision={is_revision}")


orchestrator._run_generation = fake_run_generation

# --- Mock intent classification ---
from app.engine import intent as intent_engine  # noqa: E402

async def fake_classify(user_message):
    text = user_message.lower()
    if "make it" in text or "change" in text or "bolder" in text:
        return {"intent": "REVISE", "brief": user_message}
    return {"intent": "GENERATE", "brief": user_message}

intent_engine.classify = fake_classify
orchestrator.intent_engine.classify = fake_classify

# --- Mock concept proposal decide/interpret ---
from app.engine import concept_proposal  # noqa: E402

async def fake_decide(ctx, user_message):
    text = user_message.lower()
    if "vague" in text or text.strip() == "i want something for diwali":
        return {
            "decision": "NEEDS_PROPOSAL",
            "proposal_text": "How about warm gold tones with a diya motif, your logo top-right, and a headline around your best offer? Sound good?",
            "concept_brief": "Diwali post, warm gold theme, diya motif, logo top-right, headline TBD offer",
        }
    return {"decision": "SPECIFIC_ENOUGH", "brief": user_message}

async def fake_interpret_reply(ctx, previous_proposal, client_reply):
    text = client_reply.lower().strip()
    if text in ("yes", "sounds good", "go ahead", "perfect", "yes please"):
        return {"classification": "CONFIRM"}
    return {
        "classification": "ADJUST",
        "proposal_text": f"Got it -- updated direction based on: '{client_reply}'. How's this look?",
        "concept_brief": f"Diwali post, adjusted per feedback: {client_reply}",
    }

concept_proposal.decide = fake_decide
concept_proposal.interpret_reply = fake_interpret_reply
orchestrator.concept_proposal.decide = fake_decide
orchestrator.concept_proposal.interpret_reply = fake_interpret_reply

from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile, ConversationState  # noqa: E402
from app.engine.orchestrator import generate  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402


async def run():
    phone = "919999999999"
    with get_session() as db:
        biz = Business(phone=phone, name="Copper & Crumb", industry="restaurant", onboarding_state="done")
        db.add(biz)
        db.flush()
        business_id = biz.id
        db.add(BrandProfile(business_id=business_id, tone="premium"))

    print("=" * 60)
    print("TEST 1: Vague request -> should PROPOSE, not generate")
    print("=" * 60)
    sent.clear()
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="I want something for Diwali"))
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        assert convo.pending_proposal is not None, "FAIL: no pending proposal stored!"
        assert len(sent) == 1, f"FAIL: expected 1 message sent, got {len(sent)}"
        assert "gold tones" in sent[0], "FAIL: proposal text not sent!"
        assert len(pipeline_runs) == 0, "FAIL: generation pipeline ran during proposal!"
    print("PASS: proposal stored, sent to client, NO generation ran (no pipeline log above)\n")

    print("=" * 60)
    print("TEST 2: Client gives feedback -> should ADJUST, stay pending")
    print("=" * 60)
    sent.clear()
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="can we make it more festive"))
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        assert convo.pending_proposal is not None, "FAIL: proposal should still be pending after ADJUST!"
        assert "updated direction" in sent[0], "FAIL: adjusted proposal not sent!"
        assert len(pipeline_runs) == 0, "FAIL: generation pipeline ran during ADJUST!"
    print("PASS: still pending, revised proposal sent, still NO generation ran\n")

    print("=" * 60)
    print("TEST 3: Client confirms -> should CLEAR pending, run generation")
    print("=" * 60)
    sent.clear()
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="yes please"))
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        assert convo.pending_proposal is None, "FAIL: pending_proposal should be cleared after CONFIRM!"
        assert len(pipeline_runs) == 1, f"FAIL: pipeline should have run once, ran {len(pipeline_runs)} times!"
    print("PASS: pending cleared, generation pipeline ran (see [GENERATION PIPELINE] log above)\n")

    print("=" * 60)
    print("TEST 4: Specific request -> should skip proposal, generate directly")
    print("=" * 60)
    sent.clear()
    await generate(business_id, IncomingMessage(sender=phone, type="text",
        text="Create a weekend offer post, 20% off, blue and white theme"))
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        assert convo.pending_proposal is None, "FAIL: specific request should never set a pending proposal!"
        assert len(sent) == 0, f"FAIL: no WhatsApp text should be sent for a direct generation, got {sent}"
        assert len(pipeline_runs) == 2, f"FAIL: pipeline should have run directly, ran {len(pipeline_runs)} times total!"
    print("PASS: went straight to generation pipeline, no proposal step, no extra message\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
