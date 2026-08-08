"""
Test for the per-business concurrency lock in app/router.py.

Proves the actual failure mode this fixes: two WhatsApp messages arriving
close together for the SAME business (very common — people send "wait no"
right after their first message) must be processed strictly sequentially,
not concurrently, or ConversationState reads/writes can race.

Method: patch _process_message with an artificial delay and have it record
entry/exit into a shared list. Fire two concurrent handle_message() calls
for the SAME business via asyncio.gather(). If the lock works, the order
must be [start, end, start, end] (fully serialized) — never
[start, start, end, end] (which would mean they overlapped).

A second business is included to prove the lock is per-business, not
global — its message should NOT be blocked by the first business's delay.
"""
import sys
import asyncio
import os
import time

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_concurrency.db"
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

from app import router  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402

execution_log = []


async def fake_process_message_slow(business_id, msg):
    execution_log.append(("start", str(business_id)[:8], msg.text))
    await asyncio.sleep(0.08)  # artificial delay to make a race observable
    execution_log.append(("end", str(business_id)[:8], msg.text))


router._process_message = fake_process_message_slow


async def run():
    phone_a = "919999999991"
    phone_b = "919999999990"

    print("=" * 60)
    print("TEST 1: two concurrent messages for the SAME business must serialize")
    print("=" * 60)
    execution_log.clear()

    msg1 = IncomingMessage(sender=phone_a, type="text", text="first message")
    msg2 = IncomingMessage(sender=phone_a, type="text", text="second message (sent right after)")

    start = time.monotonic()
    await asyncio.gather(
        router.handle_message(msg1),
        router.handle_message(msg2),
    )
    elapsed = time.monotonic() - start

    biz_ids_seen = {entry[1] for entry in execution_log}
    assert len(biz_ids_seen) == 1, f"FAIL: expected both messages to resolve to the same business, got {biz_ids_seen}"

    events = [e[0] for e in execution_log]
    assert events == ["start", "end", "start", "end"], (
        f"FAIL: expected fully serialized execution [start,end,start,end], got {events} "
        f"— this means the two messages overlapped, the exact race condition this lock fixes"
    )
    assert elapsed >= 0.15, f"FAIL: elapsed time {elapsed:.3f}s suggests the two 0.08s critical sections ran in parallel, not serialized"
    print(f"PASS: strictly serialized ({events}), took {elapsed:.3f}s (>= 2x0.08s, confirming no overlap)\n")

    print("=" * 60)
    print("TEST 2: a different business's message is NOT blocked by business A's lock")
    print("=" * 60)
    execution_log.clear()

    msg_a = IncomingMessage(sender=phone_a, type="text", text="business A message")
    msg_b = IncomingMessage(sender=phone_b, type="text", text="business B message")

    start = time.monotonic()
    await asyncio.gather(
        router.handle_message(msg_a),
        router.handle_message(msg_b),
    )
    elapsed = time.monotonic() - start

    # If businesses were serialized against each other too, this would take
    # >= 0.16s (two sequential 0.08s sections). Running concurrently, it
    # should take roughly one 0.08s window.
    assert elapsed < 0.15, f"FAIL: elapsed {elapsed:.3f}s suggests different businesses were serialized against each other — the lock should be per-business, not global"
    biz_ids_seen = {entry[1] for entry in execution_log}
    assert len(biz_ids_seen) == 2, f"FAIL: expected two distinct businesses processed, got {biz_ids_seen}"
    print(f"PASS: two different businesses processed concurrently in {elapsed:.3f}s (lock is per-business, not global)\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
