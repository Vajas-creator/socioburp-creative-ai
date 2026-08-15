"""
Test for the agentic-beta conversational bot (app/engine/agent.py,
app/engine/agent_tools.py, app/agentic_beta.py) -- Aug 2026, replacing the
classifier-cascade + state-machine architecture with a single continuous
Claude conversation that reasons and calls tools on its own, gated behind
an allowlist so the classic pipeline is completely unaffected for every
other business while this is validated.

Covers:
  - app/agentic_beta.py: pure membership check.
  - app/router.py: an allowlisted phone's message goes straight to
    agent.handle_message(), bypassing onboarding/classic dispatch
    entirely; a non-allowlisted phone is unaffected.
  - app/engine/agent.py: a plain text-only turn (no tool use) sends the
    final reply and persists a text-only history entry; a tool-use round
    executes the tool and continues the loop, persisting only the FINAL
    text (not the tool-call scaffolding); MAX_TOOL_ROUNDS is enforced
    rather than looping forever; an attached image is downloaded, shown
    to Claude for that turn, and NOT persisted as raw bytes in history;
    the signup bonus is granted exactly once, on first contact; an
    unhandled failure sends a generic apology + alert rather than
    crashing.
  - app/engine/agent_tools.py: generate_creative blocks a normal (non-
    allowlisted) business at 0 credits and sends topup instead of
    running the pipeline; save_logo requires an attached image;
    save_brand_info persists only the fields actually provided.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_agent.db"
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
os.environ.setdefault("R2_ACCOUNT_ID", "fake")
os.environ.setdefault("R2_ACCESS_KEY", "fake")
os.environ.setdefault("R2_SECRET_KEY", "fake")
os.environ.setdefault("R2_BUCKET", "fake")
os.environ.setdefault("R2_PUBLIC_BASE_URL", "https://fake.example.com")

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


from app import db as db_module  # noqa: E402
import app.models  # noqa: E402
db_module.Base.metadata.create_all(bind=db_module.engine)

from app import agentic_beta, router  # noqa: E402
from app.engine import agent, agent_tools  # noqa: E402
from app.whatsapp import client as wa_client  # noqa: E402
from app.db import get_session  # noqa: E402
from app.models import Business, ConversationState, CreditLedger  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402
from app.credits import get_balance  # noqa: E402

TEST_PHONE = "919818069317"  # already the agentic-beta test number
NORMAL_PHONE = "919999999970"

_REAL_EXECUTE_TOOL = agent_tools.execute_tool  # tests 6/7/9 patch this to a stub; 10/11 need the real dispatcher


def _jpeg_bytes():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, format="JPEG")
    return buf.getvalue()

sent_texts = []


async def fake_send_text(to, body):
    sent_texts.append(body)


wa_client.send_text = fake_send_text
agent.send_text = fake_send_text


def _make_business(phone):
    with get_session() as db:
        biz = Business(phone=phone, onboarding_state="new")
        db.add(biz)
        db.flush()
        return biz.id


class FakeContent:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class FakeResponse:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


def test_agentic_beta_membership():
    print("=" * 60)
    print("TEST 1: agentic_beta.is_enabled() membership check")
    print("=" * 60)
    assert agentic_beta.is_enabled(TEST_PHONE) is True
    for phone in ("918826226623", "919871583289", "919818304622"):
        assert agentic_beta.is_enabled(phone) is True, f"FAIL: expected {phone} to be in the beta"
    assert agentic_beta.is_enabled(NORMAL_PHONE) is False
    print("PASS\n")


async def test_router_dispatches_to_agent():
    print("=" * 60)
    print("TEST 2: router._process_message() sends an agentic-beta business straight to agent.handle_message()")
    print("=" * 60)
    real_handle_message = agent.handle_message  # restored below -- later tests need the real implementation
    calls = []

    async def fake_handle_message(business_id, msg):
        calls.append((business_id, msg.text))

    agent.handle_message = fake_handle_message

    biz_id = _make_business(TEST_PHONE)
    await router._process_message(biz_id, IncomingMessage(sender=TEST_PHONE, type="text", text="hi there"))
    assert calls == [(biz_id, "hi there")], f"FAIL: {calls}"
    agent.handle_message = real_handle_message
    print("PASS: routed to the agent, bypassing onboarding/classic dispatch\n")

    print("=" * 60)
    print("TEST 3: a non-allowlisted business is completely unaffected")
    print("=" * 60)
    calls.clear()

    onboarding_calls = []

    async def fake_onboarding_advance(business_id, msg):
        onboarding_calls.append((business_id, msg.text))
        return None

    router.onboarding.advance = fake_onboarding_advance

    biz_id2 = _make_business(NORMAL_PHONE)
    await router._process_message(biz_id2, IncomingMessage(sender=NORMAL_PHONE, type="text", text="hi there"))
    assert calls == [], f"FAIL: agent.handle_message() should not have run, got {calls}"
    # Falls through to the classic onboarding flow instead (mocked here --
    # onboarding.py's own real behavior is already covered by its own test files).
    assert onboarding_calls == [(biz_id2, "hi there")], f"FAIL: expected the classic onboarding flow to still run, got {onboarding_calls}"
    print("PASS: normal business still goes through the classic pipeline\n")


async def test_agent_plain_text_turn():
    print("=" * 60)
    print("TEST 4: a plain text-only turn (no tool use) sends the reply and persists text-only history")
    print("=" * 60)

    async def fake_create_message(**kwargs):
        return FakeResponse([FakeContent("text", text="Hey! What would you like me to create today?")], "end_turn")

    agent.create_message = fake_create_message

    biz_id = _make_business("919000000001")
    sent_texts.clear()
    await agent.handle_message(biz_id, IncomingMessage(sender="919000000001", type="text", text="hi"))

    assert sent_texts == ["Hey! What would you like me to create today?"], f"FAIL: {sent_texts}"
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == biz_id).first()
        assert convo.agent_message_history == [
            {"role": "user", "text": "hi"},
            {"role": "assistant", "text": "Hey! What would you like me to create today?"},
        ], f"FAIL: {convo.agent_message_history}"
    print("PASS: reply sent, text-only history persisted\n")

    print("=" * 60)
    print("TEST 5: signup bonus is granted exactly once, on first contact")
    print("=" * 60)
    assert get_balance(biz_id) == 20, f"FAIL: expected the signup bonus (20), got {get_balance(biz_id)}"

    await agent.handle_message(biz_id, IncomingMessage(sender="919000000001", type="text", text="another message"))
    assert get_balance(biz_id) == 20, f"FAIL: expected balance unchanged on a 2nd message, got {get_balance(biz_id)}"
    with get_session() as db:
        count = db.query(CreditLedger).filter(CreditLedger.business_id == biz_id).count()
        assert count == 1, f"FAIL: expected exactly 1 ledger entry, got {count}"
    print("PASS: signup bonus granted once, not repeated\n")


async def test_agent_tool_use_round():
    print("=" * 60)
    print("TEST 6: a tool-use round executes the tool and the final reply persists WITHOUT tool scaffolding")
    print("=" * 60)

    call_sequence = []

    async def fake_create_message(**kwargs):
        call_sequence.append(kwargs["messages"])
        if len(call_sequence) == 1:
            return FakeResponse(
                [FakeContent("tool_use", id="call_1", name="check_credits", input={})],
                "tool_use",
            )
        return FakeResponse([FakeContent("text", text="You've got 20 credits!")], "end_turn")

    agent.create_message = fake_create_message

    executed = []

    async def fake_execute_tool(name, args, **kwargs):
        executed.append(name)
        return "Current balance: 20 credits."

    agent_tools.execute_tool = fake_execute_tool

    biz_id = _make_business("919000000002")
    sent_texts.clear()
    await agent.handle_message(biz_id, IncomingMessage(sender="919000000002", type="text", text="how many credits do I have"))

    assert executed == ["check_credits"], f"FAIL: {executed}"
    assert sent_texts == ["You've got 20 credits!"], f"FAIL: {sent_texts}"
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == biz_id).first()
        assert convo.agent_message_history == [
            {"role": "user", "text": "how many credits do I have"},
            {"role": "assistant", "text": "You've got 20 credits!"},
        ], f"FAIL: expected only the final text persisted, got {convo.agent_message_history}"
    print("PASS: tool executed, only the final natural-language reply persisted\n")


async def test_agent_max_tool_rounds_and_failure_handling():
    print("=" * 60)
    print("TEST 7: MAX_TOOL_ROUNDS is enforced -- an infinitely tool-calling model gets a fallback reply, not an infinite loop")
    print("=" * 60)

    async def fake_create_message_infinite(**kwargs):
        return FakeResponse([FakeContent("tool_use", id="call_x", name="check_credits", input={})], "tool_use")

    agent.create_message = fake_create_message_infinite

    async def fake_execute_tool(name, args, **kwargs):
        return "Current balance: 20 credits."

    agent_tools.execute_tool = fake_execute_tool

    biz_id = _make_business("919000000003")
    sent_texts.clear()
    await agent.handle_message(biz_id, IncomingMessage(sender="919000000003", type="text", text="test"))

    assert len(sent_texts) == 1 and "rephrase" in sent_texts[0].lower(), f"FAIL: {sent_texts}"
    print(f"PASS: {sent_texts[0]!r}\n")

    print("=" * 60)
    print("TEST 8: an unhandled failure in the agent loop sends a generic apology, not a crash")
    print("=" * 60)

    async def fake_create_message_raises(**kwargs):
        raise RuntimeError("simulated failure")

    agent.create_message = fake_create_message_raises

    biz_id2 = _make_business("919000000004")
    sent_texts.clear()
    await agent.handle_message(biz_id2, IncomingMessage(sender="919000000004", type="text", text="test"))
    assert len(sent_texts) == 1 and "wrong" in sent_texts[0].lower(), f"FAIL: {sent_texts}"
    print(f"PASS: {sent_texts[0]!r}\n")


async def test_agent_image_attachment_not_persisted_as_bytes():
    print("=" * 60)
    print("TEST 9: an attached image is passed to the tool call and NOT persisted as raw bytes in history")
    print("=" * 60)

    real_jpeg_bytes = _jpeg_bytes()

    async def fake_download_media(media_id):
        return real_jpeg_bytes

    agent.download_media = fake_download_media

    received_image_bytes = []

    async def fake_create_message(**kwargs):
        # Confirm the CURRENT turn's message includes a real image content block.
        last_user_msg = kwargs["messages"][-1]
        blocks = last_user_msg["content"]
        assert isinstance(blocks, list) and any(b["type"] == "image" for b in blocks), f"FAIL: no image block in {blocks}"
        return FakeResponse([FakeContent("tool_use", id="call_logo", name="save_logo", input={"position_hint": "middle"})], "tool_use")

    agent.create_message = fake_create_message

    async def fake_execute_tool(name, args, *, current_image_bytes, **kwargs):
        received_image_bytes.append(current_image_bytes)
        return "Saved."

    agent_tools.execute_tool = fake_execute_tool

    call_count = {"n": 0}

    seen_media_type = {}

    async def fake_create_message_two_step(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            last_user_msg = kwargs["messages"][-1]
            blocks = last_user_msg["content"]
            assert isinstance(blocks, list) and any(b["type"] == "image" for b in blocks)
            image_block = next(b for b in blocks if b["type"] == "image")
            seen_media_type["value"] = image_block["source"]["media_type"]
            return FakeResponse([FakeContent("tool_use", id="call_logo", name="save_logo", input={"position_hint": "middle"})], "tool_use")
        return FakeResponse([FakeContent("text", text="Saved your logo!")], "end_turn")

    agent.create_message = fake_create_message_two_step

    biz_id = _make_business("919000000005")
    sent_texts.clear()
    await agent.handle_message(biz_id, IncomingMessage(sender="919000000005", type="image", media_id="wamid_logo", text="this is my logo"))

    assert received_image_bytes == [real_jpeg_bytes], f"FAIL: {received_image_bytes}"
    # This is the exact bug hit in production: WhatsApp photos are JPEG,
    # and Claude's API rejects a media_type that doesn't match the real
    # bytes -- see app.engine.agent._detect_image_media_type().
    assert seen_media_type["value"] == "image/jpeg", f"FAIL: expected the real JPEG bytes to be labeled image/jpeg, got {seen_media_type}"
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == biz_id).first()
        user_turn = convo.agent_message_history[0]
        assert user_turn["text"] == "[sent a photo] this is my logo", f"FAIL: {user_turn}"
        assert isinstance(user_turn["text"], str), "FAIL: history must be text-only, no embedded bytes/blocks"
    print("PASS: image bytes reached the tool call, but only a text marker was persisted\n")


async def test_tool_generate_creative_blocks_on_no_credits():
    print("=" * 60)
    print("TEST 10: agent_tools generate_creative blocks a normal business at 0 credits")
    print("=" * 60)
    agent_tools.execute_tool = _REAL_EXECUTE_TOOL
    from app.engine.context import BusinessContext
    from app import payments

    topup_calls = []

    async def fake_send_topup_options(business_id, phone, prefix=""):
        topup_calls.append(prefix)

    payments.send_topup_options = fake_send_topup_options
    agent_tools.payments.send_topup_options = fake_send_topup_options

    biz_id = _make_business("919000000006")  # NOT allowlisted, 0 credits (no signup bonus granted here)
    ctx = BusinessContext(name="Test Biz", industry="bakery")
    result = await agent_tools.execute_tool(
        "generate_creative", {"brief": "a weekend offer post", "is_revision": False},
        business_id=biz_id, phone="919000000006", ctx=ctx, last_generation_id=None, current_image_bytes=None,
    )
    assert "out of credits" in topup_calls[0].lower(), f"FAIL: {topup_calls}"
    assert "Blocked" in result, f"FAIL: {result!r}"
    print(f"PASS: {result!r}\n")


async def test_tool_save_logo_requires_image():
    print("=" * 60)
    print("TEST 11: agent_tools save_logo requires an attached image")
    print("=" * 60)
    result = await agent_tools.execute_tool(
        "save_logo", {"position_hint": "middle"},
        business_id=_make_business("919000000007"), phone="919000000007", ctx=None, last_generation_id=None,
        current_image_bytes=None,
    )
    assert "no image" in result.lower(), f"FAIL: {result!r}"
    print(f"PASS: {result!r}\n")


def test_tool_save_brand_info_partial_update():
    print("=" * 60)
    print("TEST 12: agent_tools save_brand_info persists only the fields actually provided")
    print("=" * 60)
    from app.models import BrandProfile

    biz_id = _make_business("919000000008")
    result = agent_tools._tool_save_brand_info(biz_id, {"industry": "bakery", "tone": "premium"})
    assert "industry" in result and "tone" in result, f"FAIL: {result!r}"

    with get_session() as db:
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == biz_id).first()
        assert profile.tone == "premium"
        biz = db.query(Business).filter(Business.id == biz_id).first()
        assert biz.industry == "bakery"
        assert profile.primary_color is None, "FAIL: an unprovided field should stay unset"
    print(f"PASS: {result!r}\n")


def test_detect_image_media_type():
    print("=" * 60)
    print("TEST 13: _detect_image_media_type() sniffs the real format instead of assuming PNG")
    print("=" * 60)
    import io
    from PIL import Image

    jpeg_buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(jpeg_buf, format="JPEG")
    png_buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(png_buf, format="PNG")

    assert agent._detect_image_media_type(jpeg_buf.getvalue()) == "image/jpeg"
    assert agent._detect_image_media_type(png_buf.getvalue()) == "image/png"
    assert agent._detect_image_media_type(b"not an image") == "image/jpeg", "FAIL: expected a safe default, not a raise"
    print("PASS: jpeg/png correctly distinguished, garbage input defaults safely\n")


async def run():
    test_agentic_beta_membership()
    await test_router_dispatches_to_agent()
    await test_agent_plain_text_turn()
    await test_agent_tool_use_round()
    await test_agent_max_tool_rounds_and_failure_handling()
    await test_agent_image_attachment_not_persisted_as_bytes()
    test_detect_image_media_type()
    await test_tool_generate_creative_blocks_on_no_credits()
    await test_tool_save_logo_requires_image()
    test_tool_save_brand_info_partial_update()
    print("ALL TESTS PASSED")


asyncio.run(run())
