"""
Test for the Aug 2026 "I want the AI to be smart about logo placement,
not template-y" feedback, and the deeper gap it surfaced: BrandProfile.logo_url
was NEVER written anywhere in the codebase -- onboarding dropped the logo
question in an earlier redesign and nothing replaced it, so no business
could ever actually have a logo in practice, regardless of how good the
compositing math was.

Covers:
  - app/engine/router_intent.py: LOGO_UPLOAD intent recognized (prompt
    content + fallback substring rules) for "this is my logo" style
    declarations.
  - app/engine/logo_capture.py: saves BrandProfile.logo_url and the raw
    caption as extras["logo_position_hint"]; asks for the image if none
    was attached.
  - app/router.py: routes LOGO_UPLOAD to logo_capture.handle(), and it's
    treated as a topic switch out of a pending carousel/image-intent
    negotiation (same as CANCEL/GLOBAL_COMMAND).
  - app/engine/logo_placement.py: parses a vision response into clamped
    (x, y) coordinates; fails safe to None on any error.
  - app/engine/compositor.py: smart=True uses supplied coordinates
    (clamped defensively even if a caller passes something out of
    bounds); smart=False is the original named-position behavior,
    unchanged (still used by the explicit "move my logo" revision path).
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_logo_capture_and_placement.db"
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

from PIL import Image  # noqa: E402
from app.engine import router_intent, logo_capture, logo_placement, compositor  # noqa: E402


def png_bytes(color, size=(1024, 1024)):
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def pixel(image_bytes, xy):
    return Image.open(io.BytesIO(image_bytes)).convert("RGB").getpixel(xy)


RED = (200, 50, 50)
BLUE = (30, 60, 220)


def test_router_intent_recognizes_logo_upload():
    print("=" * 60)
    print("TEST 1: router_intent's SYSTEM_PROMPT and INTENTS cover LOGO_UPLOAD")
    print("=" * 60)
    assert "LOGO_UPLOAD" in router_intent.INTENTS
    assert "logo" in router_intent.SYSTEM_PROMPT.lower()
    print("PASS\n")

    print("=" * 60)
    print("TEST 2: fallback classifier recognizes explicit logo-declaration phrasing")
    print("=" * 60)
    assert router_intent._fallback_classify("This is my logo")["intent"] == "LOGO_UPLOAD"
    assert router_intent._fallback_classify("please use this as our logo")["intent"] == "LOGO_UPLOAD"
    assert router_intent._fallback_classify("make me a Diwali post")["intent"] != "LOGO_UPLOAD"
    print("PASS\n")


async def test_logo_capture_handle():
    from app.db import get_session
    from app.models import Business, BrandProfile
    from app.schemas import IncomingMessage
    from app.whatsapp import client as wa_client

    sent = []

    async def fake_send_text(to, body):
        sent.append(body)

    wa_client.send_text = fake_send_text
    logo_capture.send_text = fake_send_text

    async def fake_download_media(media_id):
        return png_bytes(BLUE, size=(200, 200))

    logo_capture.download_media = fake_download_media

    uploaded_urls = {}

    def fake_upload_logo(business_id, image_bytes):
        url = f"https://fake.example.com/logos/{business_id}.png"
        uploaded_urls[business_id] = image_bytes
        return url

    logo_capture.upload_logo = fake_upload_logo

    with get_session() as db:
        biz = Business(phone="919999999950", name="Test Biz", industry="bakery", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id

    print("=" * 60)
    print("TEST 3: logo_capture.handle() saves logo_url and the position hint")
    print("=" * 60)
    msg = IncomingMessage(sender="919999999950", type="image", media_id="wamid_logo1", text="this is my logo, put it in the middle please")
    await logo_capture.handle(biz_id, msg)

    with get_session() as db:
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == biz_id).first()
        assert profile is not None, "FAIL: expected a BrandProfile row to exist"
        assert profile.logo_url == f"https://fake.example.com/logos/{biz_id}.png", f"FAIL: {profile.logo_url!r}"
        assert profile.extras.get("logo_position_hint") == "this is my logo, put it in the middle please", (
            f"FAIL: {profile.extras!r}"
        )
    assert any("saved your logo" in s.lower() for s in sent), f"FAIL: expected a confirmation reply, got {sent}"
    assert any("middle" in s.lower() for s in sent), f"FAIL: expected the hint echoed back, got {sent}"
    print(f"PASS: {sent[-1]!r}\n")

    print("=" * 60)
    print("TEST 4: logo_capture.handle() asks for the image if none was attached")
    print("=" * 60)
    sent.clear()
    msg2 = IncomingMessage(sender="919999999950", type="text", text="this is my logo")
    await logo_capture.handle(biz_id, msg2)
    assert len(sent) == 1 and "send" in sent[0].lower(), f"FAIL: expected an ask-to-send-image reply, got {sent}"
    print(f"PASS: {sent[0]!r}\n")


async def test_logo_placement_clamping_and_fail_safe():
    print("=" * 60)
    print("TEST 5: logo_placement.choose_position() clamps a valid response within bounds")
    print("=" * 60)

    class FakeContent:
        def __init__(self, text):
            self.text = text

    class FakeResponse:
        def __init__(self, text):
            self.content = [FakeContent(text)]

    async def fake_create_message_in_bounds(**kwargs):
        return FakeResponse('{"x": 500, "y": 600}')

    logo_placement.create_message = fake_create_message_in_bounds
    result = await logo_placement.choose_position(png_bytes(RED), 1229, 1536, 150, 150, "middle")
    assert result == (500, 600), f"FAIL: expected the in-bounds coords passed through, got {result}"
    print(f"PASS: {result}\n")

    print("=" * 60)
    print("TEST 6: logo_placement.choose_position() clamps an out-of-bounds response")
    print("=" * 60)

    async def fake_create_message_out_of_bounds(**kwargs):
        return FakeResponse('{"x": 5000, "y": -200}')

    logo_placement.create_message = fake_create_message_out_of_bounds
    result = await logo_placement.choose_position(png_bytes(RED), 1229, 1536, 150, 150, None)
    max_x = 1229 - 150 - logo_placement.MARGIN
    assert result == (max_x, logo_placement.MARGIN), f"FAIL: expected clamped coords, got {result}"
    print(f"PASS: clamped to {result}\n")

    print("=" * 60)
    print("TEST 7: logo_placement.choose_position() fails safe to None on any error")
    print("=" * 60)

    async def fake_create_message_error(**kwargs):
        raise RuntimeError("simulated API failure")

    logo_placement.create_message = fake_create_message_error
    result = await logo_placement.choose_position(png_bytes(RED), 1229, 1536, 150, 150, "corner")
    assert result is None, f"FAIL: expected None on failure, got {result}"
    print("PASS: fails safe to None\n")


async def test_compositor_smart_vs_named():
    print("=" * 60)
    print("TEST 8: compositor.composite_logo(smart=False) is unchanged (named position)")
    print("=" * 60)
    creative = png_bytes(RED, size=(1024, 1024))
    logo = png_bytes(BLUE, size=(200, 200))

    result = await compositor.composite_logo(creative, logo, position="top-left")
    assert pixel(result, (30, 30)) != RED, "FAIL: expected the logo at top-left"
    assert pixel(result, (990, 990)) == RED, "FAIL: expected bottom-right to stay plain background"
    print("PASS: named-position path unchanged\n")

    print("=" * 60)
    print("TEST 9: compositor.composite_logo(smart=True) uses logo_placement's coordinates")
    print("=" * 60)

    async def fake_choose_position(image_bytes, image_w, image_h, logo_w, logo_h, preference):
        return (400, 400)

    # compositor.py imports logo_placement LOCALLY inside composite_logo()
    # (from app.engine import logo_placement) -- that resolves the same
    # singleton module object each call, so patching the attribute on the
    # module itself (imported at the top of this file) is what actually
    # takes effect, not an attribute set on the compositor module.
    logo_placement.choose_position = fake_choose_position
    result = await compositor.composite_logo(creative, logo, smart=True, preference="middle")
    assert pixel(result, (420, 420)) != RED, "FAIL: expected the logo composited at the smart-chosen spot"
    assert pixel(result, (30, 30)) == RED, "FAIL: expected top-left to stay plain (not the named default)"
    print("PASS: smart placement used the vision-chosen coordinates\n")

    print("=" * 60)
    print("TEST 10: compositor.composite_logo(smart=True) clamps even if logo_placement misbehaves")
    print("=" * 60)

    async def fake_choose_position_bad(image_bytes, image_w, image_h, logo_w, logo_h, preference):
        return (99999, -500)  # deliberately out of bounds, simulating a bug/bad response

    logo_placement.choose_position = fake_choose_position_bad
    result = await compositor.composite_logo(creative, logo, smart=True, preference=None)
    reopened = Image.open(io.BytesIO(result))
    assert reopened.size == (1024, 1024), "FAIL: expected the output image to still be the correct canvas size"
    print("PASS: never produced an out-of-canvas paste despite a bad coordinate\n")

    print("=" * 60)
    print("TEST 11: compositor.composite_logo(smart=True) falls back to default position if logo_placement fails")
    print("=" * 60)

    async def fake_choose_position_none(image_bytes, image_w, image_h, logo_w, logo_h, preference):
        return None

    logo_placement.choose_position = fake_choose_position_none
    result = await compositor.composite_logo(creative, logo, smart=True, preference="anywhere")
    assert pixel(result, (990, 990)) != RED, "FAIL: expected the default bottom-right fallback"
    print("PASS: falls back to the named DEFAULT_POSITION on failure\n")


async def test_compositor_avoids_text_overlap():
    """
    Aug 2026 "logo overlapping my text" fix: logo_placement.py's vision
    call is told to avoid the headline text, but a vision model
    estimating pixel coordinates isn't guaranteed precise. compositor.py
    now checks the ACTUAL chosen spot against the ACTUAL drawn text rect
    (real pixel math from text_overlay.composite_headline()'s return
    value) and deterministically substitutes a clear corner if it
    collides, rather than trusting the vision call's precision alone.
    """
    creative = png_bytes(RED, size=(1024, 1024))
    logo = png_bytes(BLUE, size=(150, 150))

    print("=" * 60)
    print("TEST 13: _rects_overlap() correctly detects overlap vs. no overlap")
    print("=" * 60)
    assert compositor._rects_overlap((0, 0, 100, 100), (50, 50, 100, 100)) is True
    assert compositor._rects_overlap((0, 0, 100, 100), (100, 100, 100, 100)) is False, (
        "FAIL: rects that only touch at a corner/edge should not count as overlapping"
    )
    assert compositor._rects_overlap((0, 0, 50, 50), (500, 500, 50, 50)) is False
    print("PASS: overlap detection correct\n")

    print("=" * 60)
    print("TEST 14: composite_logo(smart=True) moves the logo off the text when the vision pick collides")
    print("=" * 60)

    async def fake_choose_position_collides(image_bytes, image_w, image_h, logo_w, logo_h, preference):
        return (700, 700)  # deliberately lands inside the text rect below

    logo_placement.choose_position = fake_choose_position_collides
    text_rect = (600, 600, 300, 300)  # overlaps (700,700)-(850,850)

    result = await compositor.composite_logo(creative, logo, smart=True, avoid_rect=text_rect)
    # The logo must NOT actually land inside the avoid_rect anywhere.
    reopened = Image.open(io.BytesIO(result)).convert("RGB")
    # Check the originally-chosen (700,700) corner is back to plain
    # background -- if the logo had stayed there, this pixel would be BLUE.
    assert reopened.getpixel((720, 720)) == RED, (
        "FAIL: expected the logo to have been moved away from the colliding spot, but it's still there"
    )
    print("PASS: colliding placement was moved off the text rect\n")

    print("=" * 60)
    print("TEST 15: composite_logo(smart=True) leaves a NON-colliding vision pick untouched")
    print("=" * 60)

    async def fake_choose_position_clear(image_bytes, image_w, image_h, logo_w, logo_h, preference):
        return (50, 50)  # nowhere near the text rect below

    logo_placement.choose_position = fake_choose_position_clear
    result = await compositor.composite_logo(creative, logo, smart=True, avoid_rect=(600, 600, 300, 300))
    reopened = Image.open(io.BytesIO(result)).convert("RGB")
    assert reopened.getpixel((70, 70)) != RED, "FAIL: a non-colliding placement should be left exactly where chosen"
    print("PASS: non-colliding placement left untouched\n")

    print("=" * 60)
    print("TEST 16: composite_logo() with avoid_rect=None behaves exactly as before (no behavior change)")
    print("=" * 60)
    logo_placement.choose_position = fake_choose_position_collides
    result_with_none = await compositor.composite_logo(creative, logo, smart=True, avoid_rect=None)
    reopened = Image.open(io.BytesIO(result_with_none)).convert("RGB")
    assert reopened.getpixel((720, 720)) != RED, (
        "FAIL: with no avoid_rect given, the vision-chosen spot should be used exactly as before"
    )
    print("PASS: avoid_rect=None is a complete no-op, matching pre-fix behavior\n")

    print("=" * 60)
    print("TEST 17: an avoid_rect covering nearly the whole canvas doesn't crash -- best-effort, keeps original spot")
    print("=" * 60)
    logo_placement.choose_position = fake_choose_position_collides
    huge_rect = (0, 0, 1024, 1024)  # nothing can avoid this
    result = await compositor.composite_logo(creative, logo, smart=True, avoid_rect=huge_rect)
    reopened = Image.open(io.BytesIO(result))
    assert reopened.size == (1024, 1024), "FAIL: should still produce a valid image, just with unavoidable overlap"
    print("PASS: pathological case handled gracefully, no crash\n")

    print("=" * 60)
    print("TEST 18: composite_logo(smart=False, named position) also respects avoid_rect")
    print("=" * 60)
    logo_placement.choose_position = fake_choose_position_collides  # irrelevant, smart=False doesn't call this
    # bottom-right named position for a 150x150 logo on a 1024x1024 canvas
    # with MARGIN=24 lands at roughly (850, 850) -- overlap it deliberately.
    result = await compositor.composite_logo(
        creative, logo, position="bottom-right", smart=False, avoid_rect=(800, 800, 224, 224),
    )
    reopened = Image.open(io.BytesIO(result)).convert("RGB")
    assert reopened.getpixel((870, 870)) == RED, (
        "FAIL: expected the named bottom-right position to also be moved off an overlapping avoid_rect"
    )
    print("PASS: named-position path also avoids overlap when avoid_rect is given\n")


async def test_router_dispatches_logo_upload():
    from app.db import get_session
    from app.models import Business, ConversationState
    from app.schemas import IncomingMessage
    from app.whatsapp import client as wa_client
    from app import router
    from app.engine import router_intent as ri, logo_capture as lc

    sent = []

    async def fake_send_text(to, body):
        sent.append(body)

    wa_client.send_text = fake_send_text
    router.send_text = fake_send_text

    async def fake_classify(text):
        if text and "logo" in text.lower():
            return {"intent": "LOGO_UPLOAD", "command": None}
        return {"intent": "OTHER", "command": None}

    ri.classify = fake_classify

    handled = []

    async def fake_handle(business_id, msg):
        handled.append((business_id, msg.text))
        await fake_send_text(msg.sender, "handled")

    lc.handle = fake_handle

    with get_session() as db:
        biz = Business(phone="919999999951", name="Test Biz", industry="bakery", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        db.add(ConversationState(business_id=biz_id, pending_carousel='{"stage": "awaiting_count"}'))

    print("=" * 60)
    print("TEST 12: router.py routes LOGO_UPLOAD to logo_capture.handle() and drops a pending carousel negotiation")
    print("=" * 60)
    msg = IncomingMessage(sender="919999999951", type="image", media_id="wamid_x", text="this is my logo")
    await router._process_message(biz_id, msg)

    assert handled == [(biz_id, "this is my logo")], f"FAIL: expected logo_capture.handle() to run, got {handled}"
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == biz_id).first()
        assert convo.pending_carousel is None, "FAIL: expected the pending carousel negotiation to be dropped (topic switch)"
    print("PASS: routed correctly, pending negotiation cleared\n")


async def run():
    test_router_intent_recognizes_logo_upload()
    await test_logo_capture_handle()
    await test_logo_placement_clamping_and_fail_safe()
    await test_compositor_smart_vs_named()
    await test_compositor_avoids_text_overlap()
    await test_router_dispatches_logo_upload()
    print("ALL TESTS PASSED")


asyncio.run(run())
