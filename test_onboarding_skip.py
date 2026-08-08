"""
Test for the onboarding "skip the guided flow" path (app/onboarding.py,
state == "new"): if the very first message already describes a real
creative request instead of just being a greeting, it's remembered
(Business.pending_first_request) and auto-generated the moment onboarding
finishes, instead of the client being forced through a generic "what's
your business name?" opener with no acknowledgment of what they asked for.

Covers:
  - A plain greeting ("hi") gets the original generic welcome, no
    pending_first_request stored.
  - A specific first request gets an acknowledging welcome AND is stored
    on pending_first_request -- the question sequence itself is unchanged
    (still asks name/industry/etc.).
  - Once onboarding completes (tone selected), a stored pending_first_request
    is cleared and auto-generation runs with that original text -- the
    client never has to repeat themselves.
  - Once onboarding completes with NO pending_first_request, no
    auto-generation runs (existing behavior unchanged).
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


async def fake_send_buttons(to, body, buttons):
    sent.append(("buttons", body))


onboarding.send_text = fake_send_text
onboarding.send_buttons = fake_send_buttons


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

generate_calls = []


async def fake_generate(business_id, msg):
    generate_calls.append((business_id, msg.text))


orchestrator.generate = fake_generate


def _make_business(phone):
    with get_session() as db:
        biz = Business(phone=phone, onboarding_state="new")
        db.add(biz)
        db.flush()
        return biz.id


async def _complete_rest_of_onboarding(biz_id, phone):
    """
    Drives name -> industry -> logo(skip) -> color_screenshot(skip) ->
    color_manual(skip) -> tone, same as any business. Skipping the
    screenshot falls through to a manual hex-code question, which itself
    needs a reply before reaching tone -- two separate skips, not one.
    """
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="Test Biz"))
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", button_id="restaurant", text="Restaurant"))
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="skip"))
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="skip"))
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="skip"))
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", button_id="friendly", text="Friendly"))


async def run():
    print("=" * 60)
    print("TEST 1: plain greeting -> generic welcome, no pending_first_request")
    print("=" * 60)
    sent.clear()
    phone = "919999999910"
    biz_id = _make_business(phone)

    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="hi"))

    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id).first()
        assert biz.pending_first_request is None, f"FAIL: expected no pending request for a greeting, got {biz.pending_first_request!r}"
        assert biz.onboarding_state == "awaiting_name"

    welcome_text = sent[-1][1]
    assert "what's your business name" in welcome_text.lower(), f"FAIL: expected the generic welcome, got {welcome_text!r}"
    assert "Got it" not in welcome_text, f"FAIL: greeting should NOT get the acknowledgment welcome, got {welcome_text!r}"
    print(f"PASS: greeting got the generic welcome, nothing stored: {welcome_text!r}\n")

    print("=" * 60)
    print("TEST 2: specific first request -> acknowledging welcome + stored on pending_first_request")
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
        assert biz.onboarding_state == "awaiting_name", "FAIL: question sequence should be unchanged (still asks name first)"

    welcome_text = sent[-1][1]
    assert "Got it" in welcome_text, f"FAIL: expected an acknowledging welcome, got {welcome_text!r}"
    print(f"PASS: specific request acknowledged and stored: welcome={welcome_text!r}\n")

    print("=" * 60)
    print("TEST 3: completing onboarding auto-generates the stored request, no repeat needed")
    print("=" * 60)
    generate_calls.clear()
    await _complete_rest_of_onboarding(biz_id2, phone2)

    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id2).first()
        assert biz.onboarding_state == "done"
        assert biz.pending_first_request is None, "FAIL: pending_first_request should be cleared once used"

    assert len(generate_calls) == 1, f"FAIL: expected exactly one auto-triggered generation, got {generate_calls}"
    assert generate_calls[0] == (biz_id2, original_request), (
        f"FAIL: expected auto-generation with the client's original words, got {generate_calls[0]}"
    )
    print(f"PASS: onboarding completion auto-generated using the original request: {generate_calls[0]}\n")

    print("=" * 60)
    print("TEST 4: completing onboarding with NO pending request -> no auto-generation")
    print("=" * 60)
    generate_calls.clear()
    await _complete_rest_of_onboarding(biz_id, phone)

    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id).first()
        assert biz.onboarding_state == "done"

    assert generate_calls == [], f"FAIL: expected no auto-generation for a greeting-only onboarding, got {generate_calls}"
    print("PASS: no pending request -> no auto-generation, existing behavior unchanged\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
