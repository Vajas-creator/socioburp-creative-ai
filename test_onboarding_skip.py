"""
Test for the onboarding "skip the guided flow" path (app/onboarding.py,
state == "new"): if the very first message already describes a real
creative request instead of just being a greeting, it's remembered
(Business.pending_first_request) and used as the auto-generation brief the
moment onboarding finishes, instead of the client having to repeat
themselves or getting a generic fallback intro-post brief. The welcome
text itself (Aug 2026 copy) is the same either way -- it already covers
"you can always just message me like you would a person" -- only whether
the request gets stored, and what brief the final auto-generation uses,
differs.

Covers:
  - A plain greeting ("hi") gets the standard welcome, no
    pending_first_request stored.
  - A specific first request gets the SAME welcome text, but IS stored on
    pending_first_request -- the question sequence itself is unchanged
    (still asks the owner's name, then "what does your business do?").
  - Once onboarding completes (Instagram step answered), a stored
    pending_first_request is used as the auto-generation brief -- the
    client never has to repeat themselves.
  - Once onboarding completes with NO pending_first_request, the
    auto-generation still fires (Sakshi said "Give me a moment" -- that's a
    promise of action), just with a generic fallback brief instead.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_onboarding_skip.db"
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

from app.db import get_session  # noqa: E402
from app.models import Business  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402
from app import onboarding  # noqa: E402
from app.engine import orchestrator  # noqa: E402

sent = []


async def fake_send_text(to, body):
    sent.append(("text", body))


onboarding.send_text = fake_send_text
onboarding.WELCOME_TO_QUESTION_DELAY_SECONDS = 0  # skip the real 1.5s pacing delay in tests


async def fake_detect_language(text):
    return "en"


async def fake_t(key, language, english_text, **kwargs):
    return english_text.format(**kwargs) if kwargs else english_text


onboarding.i18n.detect_language = fake_detect_language
onboarding.i18n.t = fake_t


async def fake_classify(user_message):
    text = (user_message or "").strip().lower()
    if text in ("hi", "hello", "hey"):
        return {"intent": "OTHER", "brief": user_message}
    return {"intent": "GENERATE", "brief": user_message}


onboarding.intent_engine.classify = fake_classify

from app.engine import brand_reflection  # noqa: E402


async def fake_understand_business(description, language="en"):
    return {
        "business_type": "bakery",
        "brand_adjectives": "warm, handcrafted",
        "business_name": None,
        "message": "Got it.\nYou run a bakery.\nI'm going to remember that your brand needs to feel warm, handcrafted — not like a mass-produced catalogue.\nOne more thing...",
    }


# onboarding.py imports brand_reflection lazily (inside the function, at
# call time) rather than at module level, so there's no onboarding.brand_reflection
# attribute to patch -- patch the actual module instead, which is the same
# object the lazy `from app.engine import brand_reflection` resolves to.
brand_reflection.understand_business = fake_understand_business

research_calls = []


async def fake_research(industry):
    research_calls.append(industry)


onboarding.industry_research.research_and_cache_if_needed = fake_research

generation_calls = []


async def fake_run_generation(business_id, phone, ctx, brief, user_message, last_generation_id, is_revision, trigger_source=None, reference_image=None):
    generation_calls.append((business_id, brief, trigger_source))


orchestrator._run_generation = fake_run_generation


def _make_business(phone):
    with get_session() as db:
        biz = Business(phone=phone, onboarding_state="new")
        db.add(biz)
        db.flush()
        return biz.id


async def _complete_rest_of_onboarding(biz_id, phone):
    """
    Drives owner-name (skipped) -> business-description -> Instagram
    (skipped), the full remaining flow. advance() itself no longer calls
    _run_generation() when onboarding completes -- it returns (ctx, brief)
    instead, and the caller (app/router.py in production) is responsible
    for invoking _run_generation(). Simulate that hand-off here, same as
    production.
    """
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="skip"))
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="I run a small bakery"))
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="skip"))
    result = await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="skip"))
    if result is not None:
        ctx, brief = result
        await orchestrator._run_generation(
            biz_id, phone, ctx, brief, brief,
            last_generation_id=None, is_revision=False,
            trigger_source="onboarding_complete",
        )


async def run():
    print("=" * 60)
    print("TEST 1: plain greeting -> standard Sakshi welcome, no pending_first_request")
    print("=" * 60)
    sent.clear()
    phone = "919999999910"
    biz_id = _make_business(phone)

    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="hi"))

    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id).first()
        assert biz.pending_first_request is None, f"FAIL: expected no pending request for a greeting, got {biz.pending_first_request!r}"
        assert biz.onboarding_state == "awaiting_owner_name"

    welcome_text = sent[0][1]
    assert welcome_text.startswith("Hi, I'm Sakshi."), f"FAIL: expected the standard Sakshi welcome, got {welcome_text!r}"
    assert sent[1][1] == "First, what's your name?", f"FAIL: expected the staged name question, got {sent[1]!r}"
    print(f"PASS: greeting got the standard welcome + staged question, nothing stored: {[s[1] for s in sent]}\n")

    print("=" * 60)
    print("TEST 2: specific first request -> SAME welcome text, but stored on pending_first_request")
    print("=" * 60)
    sent.clear()
    phone2 = "919999999911"
    biz_id2 = _make_business(phone2)
    original_request = "Create a Diwali offer post, 20% off, gold tones"

    await onboarding.advance(biz_id2, IncomingMessage(sender=phone2, type="text", text=original_request))

    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id2).first()
        assert biz.pending_first_request == original_request, (
            f"FAIL: expected the original request stored verbatim, got {biz.pending_first_request!r}"
        )
        assert biz.onboarding_state == "awaiting_owner_name", "FAIL: question sequence should be unchanged (still asks the owner's name first)"

    welcome_text = sent[0][1]
    assert welcome_text.startswith("Hi, I'm Sakshi."), f"FAIL: expected the same standard welcome, got {welcome_text!r}"
    print(f"PASS: specific request got the same welcome, but was stored: welcome={welcome_text!r}\n")

    print("=" * 60)
    print("TEST 3: completing onboarding auto-generates using the stored request, not a generic fallback")
    print("=" * 60)
    generation_calls.clear()
    await _complete_rest_of_onboarding(biz_id2, phone2)

    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id2).first()
        assert biz.onboarding_state == "done"
        assert biz.pending_first_request is None, "FAIL: pending_first_request should be cleared once used"

    assert len(generation_calls) == 1, f"FAIL: expected exactly one auto-triggered generation, got {generation_calls}"
    assert generation_calls[0] == (biz_id2, original_request, "onboarding_complete"), (
        f"FAIL: expected auto-generation with the client's original words, got {generation_calls[0]}"
    )
    print(f"PASS: onboarding completion auto-generated using the original request: {generation_calls[0]}\n")

    print("=" * 60)
    print("TEST 4: completing onboarding with NO pending request -> still auto-generates, generic fallback brief")
    print("=" * 60)
    generation_calls.clear()
    await _complete_rest_of_onboarding(biz_id, phone)

    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id).first()
        assert biz.onboarding_state == "done"

    assert len(generation_calls) == 1, f"FAIL: expected auto-generation to still fire ('Give me a moment' is a promise), got {generation_calls}"
    assert generation_calls[0][0] == biz_id and generation_calls[0][2] == "onboarding_complete"
    assert "bakery" in generation_calls[0][1].lower(), f"FAIL: expected the generic fallback to reference the extracted business type, got {generation_calls[0][1]!r}"
    print(f"PASS: no pending request -> still auto-generated, with a generic fallback brief: {generation_calls[0][1]!r}\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
