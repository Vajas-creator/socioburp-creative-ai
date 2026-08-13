"""
Test for app/router.py's handling of msg.type == "unsupported" -- the
router-side half of the "unhandled message type = total silence" fix (see
test_webhook_parse.py for the parsing-side half).

Previously a voice note, video, document, sticker, location, contact
card, etc. made parse_message() return None, and nothing downstream ever
ran -- the client got zero acknowledgment, the same failure mode as the
"uploaded image with no caption" bug fixed earlier this session.
parse_message() now returns type="unsupported" instead of None for these;
this covers what app/router.py does with that.

Covers:
  - An unsupported-type message gets an honest "can't handle that yet"
    reply -- not silence, not routed into generate()/onboarding.
  - During onboarding, an unsupported-type message still goes to
    onboarding.advance() as before (onboarding's own per-state text
    guards already handle non-text input reasonably) -- this fix doesn't
    change that priority.
  - An unsupported-type message received mid-negotiation (pending
    carousel/image-intent) still routes to that negotiation, which
    already asks for text as needed -- not silently swallowed either.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_unsupported_message_type.db"
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

from app.whatsapp import client as wa_client  # noqa: E402

sent_texts = []


async def fake_send_text(to, body):
    sent_texts.append(body)


wa_client.send_text = fake_send_text

from app import router  # noqa: E402
router.send_text = fake_send_text

onboarding_calls = []


async def fake_onboarding_advance(business_id, msg):
    onboarding_calls.append(msg.type)


router.onboarding.advance = fake_onboarding_advance

generate_calls = []


async def fake_generate(business_id, msg):
    generate_calls.append(msg.type)


import app.engine.orchestrator as orch  # noqa: E402
orch.generate = fake_generate

from app.engine import carousel  # noqa: E402
carousel.send_text = fake_send_text


async def fake_send_list(to, body, button_text, rows, section_title="Options"):
    sent_texts.append(f"[LIST] {body}")


carousel.send_list = fake_send_list

from app.db import get_session  # noqa: E402
from app.models import Business, ConversationState  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402
from app.credits import add_credits  # noqa: E402


def _make_business(phone, onboarding_state="done"):
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="salon", onboarding_state=onboarding_state)
        db.add(biz)
        db.flush()
        biz_id = biz.id
        add_credits(db, biz_id, 20, reason="signup_bonus")
        return biz_id


async def run():
    print("=" * 60)
    print("TEST 1: an unsupported-type message gets an honest reply, not silence")
    print("=" * 60)
    phone = "919999999980"
    biz_id = _make_business(phone)
    sent_texts.clear()
    generate_calls.clear()

    await router._process_message(biz_id, IncomingMessage(sender=phone, type="unsupported"))

    assert len(sent_texts) == 1, f"FAIL: expected exactly one reply, got {sent_texts}"
    assert "text messages and photos" in sent_texts[0].lower(), f"FAIL: expected the unsupported-type message, got {sent_texts[0]!r}"
    assert generate_calls == [], "FAIL: generate() should not run for an unsupported type"
    print(f"PASS: {sent_texts[0]!r}\n")

    print("=" * 60)
    print("TEST 2: during onboarding, an unsupported-type message still reaches onboarding.advance()")
    print("=" * 60)
    phone2 = "919999999981"
    biz_id2 = _make_business(phone2, onboarding_state="new")
    onboarding_calls.clear()
    sent_texts.clear()

    await router._process_message(biz_id2, IncomingMessage(sender=phone2, type="unsupported"))

    assert onboarding_calls == ["unsupported"], f"FAIL: expected onboarding.advance() to still run, got {onboarding_calls}"
    assert sent_texts == [], "FAIL: router itself should not reply directly during onboarding"
    print("PASS: onboarding priority unaffected by this fix\n")

    print("=" * 60)
    print("TEST 3: mid-carousel-negotiation, an unsupported-type message routes to the negotiation, not silence")
    print("=" * 60)
    phone3 = "919999999982"
    biz_id3 = _make_business(phone3)
    with get_session() as db:
        convo = ConversationState(business_id=biz_id3, pending_carousel='{"stage": "awaiting_count"}')
        db.add(convo)
    sent_texts.clear()

    await router._process_message(biz_id3, IncomingMessage(sender=phone3, type="unsupported"))

    assert len(sent_texts) >= 1, "FAIL: expected the carousel negotiation to reply (re-prompt for a count), not silence"
    print(f"PASS: mid-negotiation message handled, not swallowed: {sent_texts}\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
