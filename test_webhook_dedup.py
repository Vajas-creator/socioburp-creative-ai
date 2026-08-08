"""
Test for Meta webhook dedup (app/whatsapp/dedup.py + app/whatsapp/webhook.py).

Uses FastAPI's TestClient to hit the REAL POST /webhook endpoint with a
realistic Meta payload shape — not a unit-level shortcut — since the bug
this fixes is specifically about what happens at the actual HTTP boundary
Meta calls. TestClient runs BackgroundTasks synchronously within the
request/response cycle, so we can assert on handle_message call counts
directly after each POST.

Trace:
  POST with message_id="wamid.AAA" -> handle_message called once
  POST again with the SAME message_id="wamid.AAA" (simulating a Meta
    redelivery) -> handle_message NOT called again
  POST with a different message_id="wamid.BBB" -> handle_message called
    (proves dedup isn't blocking real messages, just true repeats)
"""
import sys
import os

sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_dedup.db")
os.environ["WA_VERIFY_TOKEN"] = "fake"
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

from fastapi.testclient import TestClient  # noqa: E402
from app.whatsapp import webhook, dedup  # noqa: E402

handled_messages = []


async def fake_handle_message(msg):
    handled_messages.append(msg.message_id)


webhook.handle_message = fake_handle_message

from fastapi import FastAPI  # noqa: E402
app = FastAPI()
app.include_router(webhook.router)
client = TestClient(app)


def make_payload(message_id: str, text: str, sender: str = "919999999987"):
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": message_id,
                        "from": sender,
                        "type": "text",
                        "text": {"body": text},
                    }]
                }
            }]
        }]
    }


def run():
    dedup._seen_message_ids.clear()
    handled_messages.clear()

    print("=" * 60)
    print("TEST 1: first delivery of a message -> processed")
    print("=" * 60)
    resp = client.post("/webhook", json=make_payload("wamid.AAA", "hello"))
    assert resp.status_code == 200, f"FAIL: expected 200, got {resp.status_code}"
    assert handled_messages == ["wamid.AAA"], f"FAIL: expected handle_message called once with wamid.AAA, got {handled_messages}"
    print(f"PASS: first delivery processed, handled_messages={handled_messages}\n")

    print("=" * 60)
    print("TEST 2: Meta redelivers the SAME message_id -> must NOT process again")
    print("=" * 60)
    resp = client.post("/webhook", json=make_payload("wamid.AAA", "hello"))
    assert resp.status_code == 200, f"FAIL: expected 200 even for a skipped duplicate (never tell Meta anything failed), got {resp.status_code}"
    assert handled_messages == ["wamid.AAA"], f"FAIL: expected still only ONE handle_message call (redelivery skipped), got {handled_messages}"
    print(f"PASS: redelivery correctly skipped — still only 1 total call: {handled_messages}\n")

    print("=" * 60)
    print("TEST 3: a genuinely different message_id -> processed normally")
    print("=" * 60)
    resp = client.post("/webhook", json=make_payload("wamid.BBB", "a different message"))
    assert resp.status_code == 200
    assert handled_messages == ["wamid.AAA", "wamid.BBB"], f"FAIL: expected both distinct messages processed, got {handled_messages}"
    print(f"PASS: distinct message_id processed normally — dedup isn't blocking real traffic: {handled_messages}\n")

    print("=" * 60)
    print("TEST 4: repeated redelivery of the SAME message 3 more times -> still only ever processed once")
    print("=" * 60)
    for _ in range(3):
        client.post("/webhook", json=make_payload("wamid.AAA", "hello"))
    assert handled_messages == ["wamid.AAA", "wamid.BBB"], f"FAIL: expected no additional calls from repeated redelivery, got {handled_messages}"
    print(f"PASS: 3 more redeliveries of wamid.AAA all correctly skipped: {handled_messages}\n")

    print("ALL TESTS PASSED")


run()
