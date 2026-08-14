"""
Test for the "ask for the owner's name too" personalization feature:
app/onboarding.py's new "awaiting_owner_name" state (new ->
awaiting_owner_name -> awaiting_business_description -> awaiting_instagram
-> done) and app/router.py's bare-greeting reply preferring it over the
business name.

Covers:
  - Right after the welcome message, onboarding asks for the owner's name
    before asking about the business.
  - A real name given is stored on Business.owner_name and the flow
    proceeds to the business-description question as normal.
  - A skip/decline (or empty reply) doesn't store a name and still
    proceeds -- optional, same as the Instagram question, never blocks
    onboarding completion.
  - app/router.py's bare-greeting reply uses owner_name in preference to
    the business name when both are set.
  - Falls back to the business name if only that's set, and to the fully
    generic greeting if neither is set (regression check, covered in more
    detail in test_bare_greeting.py).
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_owner_name.db"
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

sent = []


async def fake_send_text(to, body):
    sent.append(body)


wa_client.send_text = fake_send_text

from app import onboarding, router  # noqa: E402
onboarding.send_text = fake_send_text
router.send_text = fake_send_text
onboarding.WELCOME_TO_QUESTION_DELAY_SECONDS = 0

from app.engine import router_intent  # noqa: E402


async def fake_router_classify(text):
    if not text or not text.strip():
        return {"intent": "OTHER", "command": None}
    return router_intent._fallback_classify(text)


router_intent.classify = fake_router_classify


async def fake_detect_language(text):
    return "en"


async def fake_t(key, language, english_text, **kwargs):
    return english_text.format(**kwargs) if kwargs else english_text


onboarding.i18n.detect_language = fake_detect_language
onboarding.i18n.t = fake_t


async def fake_classify(user_message):
    return {"intent": "OTHER", "brief": user_message}


onboarding.intent_engine.classify = fake_classify

from app.db import get_session  # noqa: E402
from app.models import Business  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402
from app.credits import add_credits  # noqa: E402


def _make_business(phone, onboarding_state="new"):
    with get_session() as db:
        biz = Business(phone=phone, onboarding_state=onboarding_state)
        db.add(biz)
        db.flush()
        return biz.id


def _owner_name(biz_id):
    with get_session() as db:
        return db.query(Business).filter(Business.id == biz_id).first().owner_name


async def run():
    print("=" * 60)
    print("TEST 1: right after welcome, onboarding asks for the owner's name (before the business question)")
    print("=" * 60)
    sent.clear()
    phone = "919999999995"
    biz_id = _make_business(phone)

    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="hi"))

    assert sent[1] == "First, what's your name?", f"FAIL: expected the name question right after welcome, got {sent[1]!r}"
    with get_session() as db:
        assert db.query(Business).filter(Business.id == biz_id).first().onboarding_state == "awaiting_owner_name"
    print(f"PASS: {sent[1]!r}\n")

    print("=" * 60)
    print("TEST 2: giving a real name stores it and proceeds to the business question")
    print("=" * 60)
    sent.clear()
    await onboarding.advance(biz_id, IncomingMessage(sender=phone, type="text", text="Priya"))

    assert _owner_name(biz_id) == "Priya", f"FAIL: expected owner_name='Priya', got {_owner_name(biz_id)!r}"
    with get_session() as db:
        assert db.query(Business).filter(Business.id == biz_id).first().onboarding_state == "awaiting_business_description"
    assert sent == ["Let's start simple. What does your business do?"], f"FAIL: expected the business question next, got {sent}"
    print(f"PASS: owner_name stored ({_owner_name(biz_id)!r}), proceeded to the business question\n")

    print("=" * 60)
    print("TEST 3: skipping the name question doesn't store one and still proceeds")
    print("=" * 60)
    phone2 = "919999999996"
    biz_id2 = _make_business(phone2)
    sent.clear()
    await onboarding.advance(biz_id2, IncomingMessage(sender=phone2, type="text", text="hi"))
    sent.clear()
    await onboarding.advance(biz_id2, IncomingMessage(sender=phone2, type="text", text="skip"))

    assert _owner_name(biz_id2) is None, f"FAIL: expected no name stored on skip, got {_owner_name(biz_id2)!r}"
    with get_session() as db:
        assert db.query(Business).filter(Business.id == biz_id2).first().onboarding_state == "awaiting_business_description"
    print("PASS: skip -> no name stored, onboarding still proceeded\n")

    print("=" * 60)
    print("TEST 4: an empty/no-text reply also doesn't store a name and still proceeds")
    print("=" * 60)
    phone3 = "919999999997"
    biz_id3 = _make_business(phone3)
    await onboarding.advance(biz_id3, IncomingMessage(sender=phone3, type="text", text="hi"))
    await onboarding.advance(biz_id3, IncomingMessage(sender=phone3, type="text", text=None))

    assert _owner_name(biz_id3) is None
    with get_session() as db:
        assert db.query(Business).filter(Business.id == biz_id3).first().onboarding_state == "awaiting_business_description"
    print("PASS: empty reply -> no name stored, onboarding still proceeded\n")

    print("=" * 60)
    print("TEST 5: router.py's bare-greeting reply prefers owner_name over the business name")
    print("=" * 60)
    phone4 = "919999999998"
    with get_session() as db:
        biz4 = Business(phone=phone4, name="Copper & Crumb", owner_name="Ananya", onboarding_state="done")
        db.add(biz4)
        db.flush()
        biz4_id = biz4.id
        add_credits(db, biz4_id, 20, reason="signup_bonus")

    sent.clear()
    await router._process_message(biz4_id, IncomingMessage(sender=phone4, type="text", text="hi"))
    assert sent == ["Hey Ananya! How's it going? What do you want me to build today? 💡"], (
        f"FAIL: expected the owner's name preferred over the business name, got {sent}"
    )
    print(f"PASS: {sent[0]!r}\n")

    print("=" * 60)
    print("TEST 6: falls back to the business name if no owner_name is set")
    print("=" * 60)
    phone5 = "919999999999"
    with get_session() as db:
        biz5 = Business(phone=phone5, name="Copper & Crumb", owner_name=None, onboarding_state="done")
        db.add(biz5)
        db.flush()
        biz5_id = biz5.id
        add_credits(db, biz5_id, 20, reason="signup_bonus")

    sent.clear()
    await router._process_message(biz5_id, IncomingMessage(sender=phone5, type="text", text="hi"))
    assert sent == ["Hey Copper & Crumb! How's it going? What do you want me to build today? 💡"], (
        f"FAIL: expected the business name fallback, got {sent}"
    )
    print(f"PASS: {sent[0]!r}\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
