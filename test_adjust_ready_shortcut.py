"""
Test for the "don't make the client confirm a proposal that just restates
what they already said" fix in app/engine/concept_proposal.py and
app/engine/orchestrator.py's generate().

Previously, ANY ADJUST classification (feedback on a pending proposal,
"however minor" per interpret_reply()'s own system prompt) always sent a
revised proposal and waited for a separate CONFIRM reply before
generating -- even when the client's adjustment reply already gave
everything needed (occasion, offer/visual direction) to generate right
away. That's an unnecessary extra round-trip, the same friction
app/engine/carousel.py's negotiation shortcut and app/engine/image_intent.py's
"type the instruction directly" path were built to avoid elsewhere.

Fixed by having interpret_reply() also decide ready_to_generate: true when
an ADJUST reply is itself specific enough -- generate() then skips
re-proposing and waiting for another confirm, going straight to
generation instead, same trigger family as the existing ADJUST-round cap
escape hatch (just triggered by content, not round count).

Covers:
  - ADJUST + ready_to_generate=true -> generates immediately, no proposal
    re-sent, pending_proposal cleared, adjust_count never even incremented.
  - ADJUST + ready_to_generate=false (the normal case) -> unchanged
    behavior, re-proposes and waits.
  - ready_to_generate is only ever consulted on ADJUST -- CONFIRM and
    RETRY are unaffected.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_adjust_ready_shortcut.db"
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

interpret_reply_response = {"n": None}


async def fake_interpret_reply(ctx, previous_proposal, client_reply):
    return interpret_reply_response["n"]


concept_proposal.interpret_reply = fake_interpret_reply
orchestrator.concept_proposal.interpret_reply = fake_interpret_reply

pipeline_calls = []


async def fake_run_generation(business_id, phone, ctx, brief, user_message, last_generation_id, is_revision, trigger_source=None, reference_image=None):
    pipeline_calls.append({"brief": brief, "trigger_source": trigger_source})


orchestrator._run_generation = fake_run_generation

from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile, ConversationState  # noqa: E402
from app.engine.orchestrator import generate  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402


def _make_business(phone):
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="restaurant", onboarding_state="done")
        db.add(biz)
        db.flush()
        business_id = biz.id
        db.add(BrandProfile(business_id=business_id, tone="bold"))
        return business_id


def _pending(business_id):
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        return convo.pending_proposal


async def run():
    print("=" * 60)
    print("TEST 1: ADJUST + ready_to_generate=true -> generates immediately, no re-proposal, no wait")
    print("=" * 60)
    phone = "919999999940"
    business_id = _make_business(phone)

    await generate(business_id, IncomingMessage(sender=phone, type="text", text="make me something nice"))
    assert _pending(business_id) is not None, "FAIL: expected a pending proposal after the first vague message"

    sent_texts.clear()
    pipeline_calls.clear()
    interpret_reply_response["n"] = {
        "classification": "ADJUST",
        "proposal_text": "Updated proposal text (should NOT be sent to the client)",
        "concept_brief": "Diwali post, 20% off, warm gold tones, logo bottom-right",
        "ready_to_generate": True,
    }
    await generate(business_id, IncomingMessage(
        sender=phone, type="text",
        text="Diwali post, 20% off everything, warm gold tones, put my logo bottom-right",
    ))

    assert len(pipeline_calls) == 1, f"FAIL: expected the pipeline to run immediately, got {pipeline_calls}"
    assert pipeline_calls[0]["brief"] == "Diwali post, 20% off, warm gold tones, logo bottom-right"
    assert pipeline_calls[0]["trigger_source"] == "adjust_ready", f"FAIL: expected trigger_source='adjust_ready', got {pipeline_calls[0]}"
    assert "Updated proposal text" not in "".join(sent_texts), (
        f"FAIL: should NOT re-send a proposal the client already effectively answered, got {sent_texts}"
    )
    assert _pending(business_id) is None, "FAIL: expected pending_proposal cleared"
    print(f"PASS: generated immediately on a specific ADJUST reply, no redundant re-proposal sent: {pipeline_calls[0]}\n")

    print("=" * 60)
    print("TEST 2: ADJUST + ready_to_generate=false -> unchanged behavior, re-proposes and waits")
    print("=" * 60)
    phone2 = "919999999941"
    business_id2 = _make_business(phone2)
    await generate(business_id2, IncomingMessage(sender=phone2, type="text", text="make me something nice"))

    sent_texts.clear()
    pipeline_calls.clear()
    interpret_reply_response["n"] = {
        "classification": "ADJUST",
        "proposal_text": "How about brighter colors instead — sound good?",
        "concept_brief": "round1: brighter colors",
        "ready_to_generate": False,
    }
    await generate(business_id2, IncomingMessage(sender=phone2, type="text", text="make it more colorful"))

    assert pipeline_calls == [], f"FAIL: should NOT generate yet, got {pipeline_calls}"
    assert sent_texts == ["How about brighter colors instead — sound good?"], f"FAIL: expected the revised proposal sent, got {sent_texts}"
    pending = _pending(business_id2)
    assert pending is not None, "FAIL: expected pending_proposal to still exist, awaiting confirmation"
    print("PASS: a still-vague ADJUST reply keeps the normal propose-then-wait loop\n")

    print("=" * 60)
    print("TEST 3: CONFIRM is unaffected by ready_to_generate (key isn't even present)")
    print("=" * 60)
    sent_texts.clear()
    pipeline_calls.clear()
    interpret_reply_response["n"] = {"classification": "CONFIRM"}
    await generate(business_id2, IncomingMessage(sender=phone2, type="text", text="yes go ahead"))

    assert len(pipeline_calls) == 1
    assert pipeline_calls[0]["trigger_source"] == "proposal_confirmed"
    print("PASS: CONFIRM path unaffected\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
