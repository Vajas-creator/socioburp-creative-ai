"""
Test for qa/customer_simulator.py -- the on-demand "act as a real
customer, then review like an expert" QA tool (Aug 2026, per Vajas's
request). This is a different kind of test than most files here: the
simulator itself runs the REAL pipeline with REAL API keys when actually
invoked (see .github/workflows/customer-simulation.yml) -- there is no
code branch to meaningfully test the live model's judgment against. What
IS worth locking in with fast, free, mocked tests is the simulator's OWN
logic: does it parse the customer-AI's response correctly, does it fail
safe when a call errors, does it build a correct transcript and report.

Covers:
  - _customer_says() parses a well-formed customer-AI response, and
    fails safe to the goal text verbatim (attach_image=None) on any
    error -- a scenario must never just stop because one turn's API
    call hiccuped.
  - run_scenario() drives router.handle_message() for each goal and
    accumulates a transcript with both speakers' turns.
  - run_expert_review() builds vision content blocks only for turns that
    actually delivered an image, and fails safe to a placeholder string
    (not an exception) if the review call itself fails.
  - _transcript_to_markdown() and _send_telegram_summary() (no-op
    without Telegram config; sends via a mocked httpx client otherwise).
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_customer_simulator.db"
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
os.environ.setdefault("IMAGE_API_KEY", "fake")

sys.path.insert(0, "qa")
import customer_simulator as sim  # noqa: E402

from app.config import settings  # noqa: E402


class FakeContent:
    def __init__(self, text):
        self.text = text


class FakeResponse:
    def __init__(self, text):
        self.content = [FakeContent(text)]


async def test_customer_says_parses_and_fails_safe():
    print("=" * 60)
    print("TEST 1: _customer_says() parses a well-formed response")
    print("=" * 60)

    async def fake_create_message(**kwargs):
        return FakeResponse('{"message": "hii what can u make for me", "attach_image": null}')

    sim.create_message = fake_create_message
    persona = sim.PERSONAS[0]
    message, attach = await sim._customer_says(persona, [], "Say hi and see what this bot does.")
    assert message == "hii what can u make for me", f"FAIL: {message!r}"
    assert attach is None
    print(f"PASS: {message!r}, attach={attach}\n")

    print("=" * 60)
    print("TEST 2: _customer_says() picks up an attach_image value")
    print("=" * 60)

    async def fake_create_message_img(**kwargs):
        return FakeResponse('{"message": "here is my logo, put it in the corner", "attach_image": "logo"}')

    sim.create_message = fake_create_message_img
    message, attach = await sim._customer_says(persona, [], "send your logo")
    assert attach == "logo", f"FAIL: {attach!r}"
    print(f"PASS: attach={attach}\n")

    print("=" * 60)
    print("TEST 3: _customer_says() fails safe to the goal text verbatim on error")
    print("=" * 60)

    async def fake_create_message_error(**kwargs):
        raise RuntimeError("simulated failure")

    sim.create_message = fake_create_message_error
    goal = "Ask for a weekend offer post"
    message, attach = await sim._customer_says(persona, [], goal)
    assert message == goal, f"FAIL: expected the goal text verbatim, got {message!r}"
    assert attach is None
    print(f"PASS: fell back to {message!r}\n")


async def test_run_scenario_builds_transcript():
    print("=" * 60)
    print("TEST 4: run_scenario() drives router.handle_message() and builds a two-sided transcript")
    print("=" * 60)

    async def fake_customer_says(persona, transcript, goal):
        return f"[as customer] {goal}", None

    sim._customer_says = fake_customer_says

    calls = []

    async def fake_handle_message(msg):
        calls.append(msg.text)
        await sim._fake_send_text(msg.sender, f"Sakshi replying to: {msg.text}")

    sim.router.handle_message = fake_handle_message

    persona = {
        "name": "Test Persona",
        "phone": "919999999960",
        "description": "A test persona.",
        "goals": ["first goal", "second goal"],
    }
    transcript = await sim.run_scenario(persona)

    assert calls == ["[as customer] first goal", "[as customer] second goal"], f"FAIL: {calls}"
    speakers = [t["speaker"] for t in transcript]
    assert speakers == ["customer", "sakshi", "customer", "sakshi"], f"FAIL: {speakers}"
    assert transcript[1]["text"] == "Sakshi replying to: [as customer] first goal", f"FAIL: {transcript[1]}"
    print(f"PASS: transcript has {len(transcript)} turns in the right order\n")

    print("=" * 60)
    print("TEST 5: run_scenario() doesn't crash the whole scenario if handle_message() raises")
    print("=" * 60)

    async def fake_handle_message_raises(msg):
        raise RuntimeError("simulated pipeline crash")

    sim.router.handle_message = fake_handle_message_raises
    transcript2 = await sim.run_scenario(persona)
    assert len(transcript2) == 4, f"FAIL: expected 2 customer + 2 fallback sakshi turns, got {len(transcript2)}"
    assert "NO REPLY" in transcript2[1]["text"], f"FAIL: {transcript2[1]}"
    print("PASS: a pipeline crash on one turn produces a placeholder reply, not a dead scenario\n")


async def test_expert_review_vision_blocks_and_fail_safe():
    print("=" * 60)
    print("TEST 6: run_expert_review() includes an image block only for turns with image_bytes")
    print("=" * 60)

    captured_kwargs = {}

    async def fake_create_message(**kwargs):
        captured_kwargs.update(kwargs)
        return FakeResponse("## Summary\nLooks fine.")

    sim.create_message = fake_create_message

    transcript = [
        {"speaker": "customer", "text": "hi", "image_bytes": None},
        {"speaker": "sakshi", "text": "here's your post", "image_bytes": sim._PLACEHOLDER_IMAGES["product_photo"]},
    ]
    review = await sim.run_expert_review(sim.PERSONAS[0], transcript)
    assert "Looks fine" in review
    content_blocks = captured_kwargs["messages"][0]["content"]
    image_blocks = [b for b in content_blocks if b["type"] == "image"]
    assert len(image_blocks) == 1, f"FAIL: expected exactly 1 image block, got {len(image_blocks)}"
    print(f"PASS: {len(content_blocks)} content blocks, {len(image_blocks)} image block\n")

    print("=" * 60)
    print("TEST 7: run_expert_review() fails safe (returns a string, doesn't raise) on API failure")
    print("=" * 60)

    async def fake_create_message_error(**kwargs):
        raise RuntimeError("simulated failure")

    sim.create_message = fake_create_message_error
    review2 = await sim.run_expert_review(sim.PERSONAS[0], transcript)
    assert isinstance(review2, str) and len(review2) > 0, f"FAIL: {review2!r}"
    print(f"PASS: {review2!r}\n")


def test_transcript_to_markdown():
    print("=" * 60)
    print("TEST 8: _transcript_to_markdown() formats both speakers and flags image turns")
    print("=" * 60)
    transcript = [
        {"speaker": "customer", "text": "hi", "image_bytes": None},
        {"speaker": "sakshi", "text": "here's your post", "image_bytes": b"fakebytes"},
    ]
    md = sim._transcript_to_markdown(transcript)
    assert "**Customer**: hi" in md, f"FAIL: {md!r}"
    assert "**Sakshi**: here's your post" in md and "image delivered" in md, f"FAIL: {md!r}"
    print(f"PASS:\n{md}\n")


async def test_telegram_summary_noop_and_send():
    print("=" * 60)
    print("TEST 9: _send_telegram_summary() is a silent no-op with no Telegram config")
    print("=" * 60)
    settings.ALERT_TELEGRAM_TOKEN = ""
    settings.ALERT_TELEGRAM_CHAT_ID = ""

    calls = []

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            calls.append(json)

    import httpx
    sim_httpx_patch = httpx.AsyncClient
    httpx.AsyncClient = FakeAsyncClient

    await sim._send_telegram_summary(["line 1", "line 2"])
    assert calls == [], f"FAIL: expected no HTTP call, got {calls}"
    print("PASS: no-op without config\n")

    print("=" * 60)
    print("TEST 10: _send_telegram_summary() sends when configured")
    print("=" * 60)
    settings.ALERT_TELEGRAM_TOKEN = "fake-token"
    settings.ALERT_TELEGRAM_CHAT_ID = "12345"

    await sim._send_telegram_summary(["hello", "world"])
    assert len(calls) == 1, f"FAIL: {calls}"
    assert calls[0]["chat_id"] == "12345"
    assert "hello" in calls[0]["text"] and "world" in calls[0]["text"]
    print(f"PASS: {calls[0]}\n")

    httpx.AsyncClient = sim_httpx_patch


async def run():
    await test_customer_says_parses_and_fails_safe()
    await test_run_scenario_builds_transcript()
    await test_expert_review_vision_blocks_and_fail_safe()
    test_transcript_to_markdown()
    await test_telegram_summary_noop_and_send()
    print("ALL TESTS PASSED")


asyncio.run(run())
