"""
Regression test for app/history.py's send_recent_generations().

Previously it fetched Generation rows inside `with get_session() as db:`
but read .image_url/.caption on them AFTER the block exited. Since
get_session() commits (expiring attributes by default) and closes the
session on exit, that access raised
sqlalchemy.orm.exc.DetachedInstanceError whenever a client sent the
"history" WhatsApp command — see app/router.py's "history" handler.

Covers:
  - 3 "done" generations -> all 3 delivered, most recent first, no
    DetachedInstanceError
  - a business with no generations -> the "no creatives yet" message,
    not a crash
  - a "pending"/non-"done" generation is excluded (status filter still
    works after the fix)
"""
import sys
import asyncio
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_history.db"
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
from app.models import Business, Generation  # noqa: E402
from app import history  # noqa: E402

sent = []


async def fake_send_image(to, url, caption=""):
    sent.append(("image", url, caption))


async def fake_send_text(to, body):
    sent.append(("text", body))


history.send_image = fake_send_image
history.send_text = fake_send_text


def _make_business(phone):
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="bakery", onboarding_state="done")
        db.add(biz)
        db.flush()
        return biz.id


async def run():
    print("=" * 60)
    print("TEST 1: 3 done generations -> all delivered, most recent first, no DetachedInstanceError")
    print("=" * 60)
    sent.clear()
    biz_id = _make_business("919999999900")
    base_time = datetime.now(timezone.utc)
    with get_session() as db:
        for i in range(3):
            # Explicit, strictly increasing created_at -- SQLite's func.now()
            # is only second-resolution, so back-to-back inserts in the same
            # transaction can tie and make ordering non-deterministic here;
            # that's a test-harness artifact, not something this test is
            # meant to exercise.
            db.add(Generation(
                business_id=biz_id, user_message=f"m{i}", status="done",
                image_url=f"http://img/{i}.png", caption=f"cap{i}",
                created_at=base_time + timedelta(seconds=i),
            ))

    await history.send_recent_generations(biz_id, "919999999900")

    assert len(sent) == 3, f"FAIL: expected 3 sends, got {sent}"
    assert all(kind == "image" for kind, *_ in sent), f"FAIL: expected all image sends, got {sent}"
    urls = [url for _, url, _ in sent]
    assert urls == ["http://img/2.png", "http://img/1.png", "http://img/0.png"], (
        f"FAIL: expected most-recent-first order, got {urls}"
    )
    print(f"PASS: all 3 delivered in correct order with no DetachedInstanceError: {urls}\n")

    print("=" * 60)
    print("TEST 2: no generations -> 'no creatives yet' message, not a crash")
    print("=" * 60)
    sent.clear()
    empty_biz_id = _make_business("919999999901")

    await history.send_recent_generations(empty_biz_id, "919999999901")

    assert len(sent) == 1 and sent[0][0] == "text", f"FAIL: expected a single text fallback, got {sent}"
    print(f"PASS: empty history correctly fell back to a text message: {sent[0][1]!r}\n")

    print("=" * 60)
    print("TEST 3: non-'done' generation is excluded")
    print("=" * 60)
    sent.clear()
    pending_biz_id = _make_business("919999999902")
    with get_session() as db:
        db.add(Generation(
            business_id=pending_biz_id, user_message="m", status="pending",
            image_url="http://img/pending.png", caption="cap",
        ))

    await history.send_recent_generations(pending_biz_id, "919999999902")

    assert len(sent) == 1 and sent[0][0] == "text", f"FAIL: expected pending generation excluded (text fallback), got {sent}"
    print("PASS: non-'done' generation correctly excluded from history\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
