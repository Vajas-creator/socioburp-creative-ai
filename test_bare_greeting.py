"""
Test for the recurring bare-greeting prompt (app/router.py:
BARE_GREETINGS + the check in _process_message()).

Previously a bare "hi" from an already-onboarded client fell all the way
through to orchestrator.generate() -> intent classification -> the
generic OTHER-intent fallback ("I'm Maya, your creative partner here! Try
something like..."). Now it's intercepted early with a short, direct
prompt, without ever reaching intent classification or (for a genuinely
new business) re-running onboarding.

Covers:
  - A returning user (onboarding_state == "done") sending "hi"/"hey"/
    "hello" (any case/whitespace, and with trailing punctuation like
    "Hello!" or "hey??") gets the short prompt directly --
    onboarding.advance() is NOT called, orchestrator.generate() is NOT
    called.
  - A genuinely new business (onboarding_state != "done") sending "hi"
    still goes to onboarding.advance() as before -- the greeting
    shortcut only applies to already-onboarded businesses.
  - A returning user's real request (not a bare greeting) still reaches
    orchestrator.generate() as usual -- no regression.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_bare_greeting.db"
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
from app import router  # noqa: E402
from app.db import get_session  # noqa: E402
from app.models import Business  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402
from app.credits import add_credits  # noqa: E402

sent = []


async def fake_send_text(to, body):
    sent.append(body)


wa_client.send_text = fake_send_text
router.send_text = fake_send_text

onboarding_calls = []


async def fake_onboarding_advance(business_id, msg):
    onboarding_calls.append(msg.text)


router.onboarding.advance = fake_onboarding_advance

generate_calls = []


async def fake_generate(business_id, msg):
    generate_calls.append(msg.text)


import app.engine.orchestrator as orch  # noqa: E402
orch.generate = fake_generate


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
    print("TEST 1: returning user sends a bare greeting -> short prompt, no onboarding, no generate()")
    print("=" * 60)
    for i, greeting in enumerate(["hi", "Hi", "HELLO", " hey  ", "hii", "Hello!", "hi.", "hey??", "Hola~"]):
        sent.clear()
        onboarding_calls.clear()
        generate_calls.clear()
        phone = f"91999999995{i}"
        biz_id = _make_business(phone)

        await router._process_message(biz_id, IncomingMessage(sender=phone, type="text", text=greeting))

        assert len(sent) == 1, f"FAIL ({greeting!r}): expected exactly 1 message, got {sent}"
        assert sent[0] == "Hey! Want today's post? I've got an idea. 💡", f"FAIL ({greeting!r}): expected the short creative prompt, got {sent[0]!r}"
        assert onboarding_calls == [], f"FAIL ({greeting!r}): onboarding should NOT run for a returning user, got {onboarding_calls}"
        assert generate_calls == [], f"FAIL ({greeting!r}): generate() should NOT run for a bare greeting, got {generate_calls}"
        print(f"PASS ({greeting!r}): {sent[0]!r}")
    print()

    print("=" * 60)
    print("TEST 2: genuinely new business sends 'hi' -> still routes to onboarding, NOT the short prompt")
    print("=" * 60)
    sent.clear()
    onboarding_calls.clear()
    generate_calls.clear()
    phone2 = "919999999960"
    biz_id2 = _make_business(phone2, onboarding_state="new")

    await router._process_message(biz_id2, IncomingMessage(sender=phone2, type="text", text="hi"))

    assert onboarding_calls == ["hi"], f"FAIL: expected onboarding.advance() called with 'hi', got {onboarding_calls}"
    assert sent == [], f"FAIL: router itself should not have sent anything directly, got {sent}"
    assert generate_calls == [], f"FAIL: generate() should not run during onboarding, got {generate_calls}"
    print("PASS: new business's greeting correctly routed to onboarding, not the short prompt\n")

    print("=" * 60)
    print("TEST 3: returning user's real request still reaches generate() as usual")
    print("=" * 60)
    sent.clear()
    onboarding_calls.clear()
    generate_calls.clear()
    phone3 = "919999999961"
    biz_id3 = _make_business(phone3)

    await router._process_message(biz_id3, IncomingMessage(sender=phone3, type="text", text="Create a weekend offer post"))

    assert generate_calls == ["Create a weekend offer post"], f"FAIL: expected generate() called with the real request, got {generate_calls}"
    assert onboarding_calls == [], f"FAIL: onboarding should not run for a returning user, got {onboarding_calls}"
    print("PASS: real request unaffected, still reaches generate()\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
