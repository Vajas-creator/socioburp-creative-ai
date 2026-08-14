"""
Test for app/analytics.py and its four wiring points: signup
(app/router.py), onboarding_completed (app/onboarding.py),
first_creative_approved (app/engine/learning.py), and
user_returned_voluntarily (app/router.py, heuristic-based).

Covers:
  - A brand-new phone number logs exactly one 'signup' event; a second
    message from the SAME phone does not log a second one.
  - Onboarding completing logs exactly one 'onboarding_completed' event.
  - The first real accept signal for a business logs
    'first_creative_approved'; a SECOND accept for the same business does
    NOT log it again.
  - A returning user's message logs 'user_returned_voluntarily' only when
    enough time has passed since their last logged event -- not on every
    message.
  - log_event() fails safe: a broken DB write never raises out to the
    caller.
"""
import sys
import asyncio
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_analytics_events.db"
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
from app.models import Business, BrandProfile, Generation, AnalyticsEvent  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402
from app import analytics, router  # noqa: E402
from app.credits import add_credits  # noqa: E402

sent = []


async def fake_send_text(to, body):
    sent.append(body)


from app.whatsapp import client as wa_client  # noqa: E402
wa_client.send_text = fake_send_text
router.send_text = fake_send_text

from app.engine import router_intent  # noqa: E402


async def fake_router_classify(text):
    if not text or not text.strip():
        return {"intent": "OTHER", "command": None}
    return router_intent._fallback_classify(text)


router_intent.classify = fake_router_classify


async def fake_onboarding_advance(business_id, msg):
    pass


# router.onboarding IS the same module object as `from app import onboarding`
# (modules are singletons) -- this patch is GLOBAL, not router-scoped, so
# it must be restored (see TEST 3) before anything calls the real
# onboarding.advance() again.
real_onboarding_advance = router.onboarding.advance
router.onboarding.advance = fake_onboarding_advance


async def fake_generate(business_id, msg):
    pass


import app.engine.orchestrator as orch  # noqa: E402
orch.generate = fake_generate


def events_for(business_id, event_type=None):
    with get_session() as db:
        q = db.query(AnalyticsEvent).filter(AnalyticsEvent.business_id == business_id)
        if event_type:
            q = q.filter(AnalyticsEvent.event_type == event_type)
        return q.all()


async def run():
    print("=" * 60)
    print("TEST 1: brand-new phone logs exactly one 'signup' event")
    print("=" * 60)
    phone = "919999999960"
    msg = IncomingMessage(sender=phone, type="text", text="hi")
    await router.handle_message(msg)

    with get_session() as db:
        biz = db.query(Business).filter(Business.phone == phone).first()
        biz_id = biz.id

    signups = events_for(biz_id, "signup")
    assert len(signups) == 1, f"FAIL: expected exactly 1 signup event, got {len(signups)}"
    print(f"PASS: 1 signup event logged for {phone}\n")

    print("=" * 60)
    print("TEST 2: a second message from the SAME phone does NOT log another signup")
    print("=" * 60)
    with get_session() as db:
        db.query(Business).filter(Business.id == biz_id).update({"onboarding_state": "done"})
        add_credits(db, biz_id, 20, reason="signup_bonus")

    await router.handle_message(IncomingMessage(sender=phone, type="text", text="another message"))

    signups = events_for(biz_id, "signup")
    assert len(signups) == 1, f"FAIL: expected still exactly 1 signup event, got {len(signups)}"
    print("PASS: no duplicate signup event for a returning message\n")

    print("=" * 60)
    print("TEST 3: onboarding completing logs exactly one 'onboarding_completed' event")
    print("=" * 60)
    from app import onboarding
    onboarding.advance = real_onboarding_advance
    router.onboarding.advance = real_onboarding_advance
    onboarding.send_text = fake_send_text

    async def fake_detect_language(text):
        return "en"

    async def fake_t(key, language, english_text, **kwargs):
        return english_text.format(**kwargs) if kwargs else english_text

    onboarding.i18n.detect_language = fake_detect_language
    onboarding.i18n.t = fake_t
    onboarding.WELCOME_TO_QUESTION_DELAY_SECONDS = 0

    async def fake_classify(user_message):
        return {"intent": "OTHER", "brief": user_message}

    onboarding.intent_engine.classify = fake_classify

    from app.engine import brand_reflection

    async def fake_understand_business(description, language="en"):
        return {"business_type": "bakery", "brand_adjectives": "warm", "business_name": None, "message": "Got it.\nYou run a bakery.\nOne more thing..."}

    brand_reflection.understand_business = fake_understand_business

    async def fake_research(industry):
        pass

    onboarding.industry_research.research_and_cache_if_needed = fake_research

    async def fake_run_generation(business_id, phone, ctx, brief, user_message, last_generation_id, is_revision, trigger_source=None, reference_image=None):
        pass

    orch._run_generation = fake_run_generation

    phone2 = "919999999961"
    with get_session() as db:
        biz2 = Business(phone=phone2, onboarding_state="new")
        db.add(biz2)
        db.flush()
        biz2_id = biz2.id

    await onboarding.advance(biz2_id, IncomingMessage(sender=phone2, type="text", text="hi"))
    await onboarding.advance(biz2_id, IncomingMessage(sender=phone2, type="text", text="skip"))  # owner-name question
    await onboarding.advance(biz2_id, IncomingMessage(sender=phone2, type="text", text="I run a bakery"))
    await onboarding.advance(biz2_id, IncomingMessage(sender=phone2, type="text", text="skip"))  # instagram question
    await onboarding.advance(biz2_id, IncomingMessage(sender=phone2, type="text", text="skip"))  # brand-details question

    completed = events_for(biz2_id, "onboarding_completed")
    assert len(completed) == 1, f"FAIL: expected exactly 1 onboarding_completed event, got {len(completed)}"
    print("PASS: 1 onboarding_completed event logged\n")

    print("=" * 60)
    print("TEST 4: first accept signal logs 'first_creative_approved'; a second accept does NOT repeat it")
    print("=" * 60)
    from app.engine import learning

    phone3 = "919999999962"
    with get_session() as db:
        biz3 = Business(phone=phone3, name="Test Biz", industry="bakery", onboarding_state="done")
        db.add(biz3)
        db.flush()
        biz3_id = biz3.id
        db.add(BrandProfile(business_id=biz3_id))
        gen1 = Generation(business_id=biz3_id, user_message="Create a post", status="done", quality_score=90, credits_charged=1)
        gen2 = Generation(business_id=biz3_id, user_message="Create another post", status="done", quality_score=90, credits_charged=1)
        db.add(gen1)
        db.add(gen2)
        db.flush()
        gen1_id, gen2_id = gen1.id, gen2.id

    await learning.record_accepted_direction(biz3_id, gen1_id)
    approved = events_for(biz3_id, "first_creative_approved")
    assert len(approved) == 1, f"FAIL: expected exactly 1 first_creative_approved event after the first accept, got {len(approved)}"
    print("PASS: first accept logged first_creative_approved\n")

    await learning.record_accepted_direction(biz3_id, gen2_id)
    approved = events_for(biz3_id, "first_creative_approved")
    assert len(approved) == 1, f"FAIL: expected STILL exactly 1 first_creative_approved event after a second accept, got {len(approved)}"
    print("PASS: second accept did NOT log a duplicate first_creative_approved\n")

    print("=" * 60)
    print("TEST 5: user_returned_voluntarily fires only after the gap threshold, not on every message")
    print("=" * 60)
    phone4 = "919999999963"
    with get_session() as db:
        biz4 = Business(phone=phone4, name="Test Biz", industry="bakery", onboarding_state="done")
        db.add(biz4)
        db.flush()
        biz4_id = biz4.id
        add_credits(db, biz4_id, 20, reason="signup_bonus")
        # Simulate a recent event -- no return should be logged yet.
        db.add(AnalyticsEvent(business_id=biz4_id, event_type="signup", created_at=datetime.now(timezone.utc)))

    await router._process_message(biz4_id, IncomingMessage(sender=phone4, type="text", text="hi again"))
    returns = events_for(biz4_id, "user_returned_voluntarily")
    assert returns == [], f"FAIL: expected no return event so soon after the last one, got {len(returns)}"
    print("PASS: no return event logged when the last event was recent\n")

    with get_session() as db:
        # Clear the recent event from above -- otherwise it would still be
        # the most recent row and this "old" one would never be picked up
        # by the "most recent event" query.
        db.query(AnalyticsEvent).filter(AnalyticsEvent.business_id == biz4_id).delete()
        old_time = datetime.now(timezone.utc) - timedelta(hours=analytics.RETURN_GAP_HOURS + 1)
        db.add(AnalyticsEvent(business_id=biz4_id, event_type="signup", created_at=old_time))

    await router._process_message(biz4_id, IncomingMessage(sender=phone4, type="text", text="hi again"))
    returns = events_for(biz4_id, "user_returned_voluntarily")
    assert len(returns) == 1, f"FAIL: expected exactly 1 return event once past the gap threshold, got {len(returns)}"
    print(f"PASS: return event logged once past the {analytics.RETURN_GAP_HOURS}h threshold\n")

    print("=" * 60)
    print("TEST 6: log_event() fails safe -- a broken DB write never raises out to the caller")
    print("=" * 60)
    import app.analytics as analytics_module

    def broken_get_session():
        raise RuntimeError("simulated DB failure")

    real_get_session = analytics_module.get_session
    analytics_module.get_session = broken_get_session
    try:
        analytics.log_event(biz4_id, "signup")  # must not raise
        print("PASS: log_event() swallowed the DB failure without raising\n")
    finally:
        analytics_module.get_session = real_get_session

    print("ALL TESTS PASSED")


asyncio.run(run())
