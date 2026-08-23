"""
Test for the Aug 2026 "interpreting an image shared unprompted" fix in
app/engine/image_intent.py (Priority 2 of the live-test follow-up list).

Previously EVERY unprompted image (no caption) got the identical fixed
"Change background / Use as-is / Something else" menu, regardless of what
the image actually showed -- a screenshot of a competitor's post, a menu,
a flyer got asked the same edit-menu question as a photo of the client's
own product. _understand_image() now looks at the image first via Claude
vision; only a genuine edit-candidate (or something the vision call
genuinely can't classify, or a vision-call failure) falls through to the
existing menu -- informative content gets a real, grounded response
instead.

Covers:
  - _understand_image(): EDIT_CANDIDATE, INFORMATIVE (with a real
    response), and AMBIGUOUS classifications are all parsed correctly;
    any failure (bad JSON, API error, unexpected category value) fails
    safe to AMBIGUOUS/None rather than raising.
  - start(): an INFORMATIVE image sends the vision-grounded response
    directly and does NOT enter the pending_image_intent negotiation
    (no button menu).
  - start(): an EDIT_CANDIDATE or AMBIGUOUS image still falls through to
    the existing button-menu flow, unchanged.
  - start(): the image is only downloaded ONCE (via _persist_photo),
    not re-downloaded separately for the vision call -- a regression
    here would silently double WhatsApp media-download calls on every
    unprompted image.
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_image_intent_understanding.db"
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

from PIL import Image  # noqa: E402
from app.whatsapp import client as wa_client  # noqa: E402
from app.engine import image_intent, router_intent  # noqa: E402
from app.engine.context import BusinessContext  # noqa: E402
from app import router  # noqa: E402


def png_bytes(color=(120, 60, 200), size=(64, 64)):
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


REFERENCE_PHOTO_BYTES = png_bytes()

sent_texts, sent_buttons_calls = [], []


async def fake_send_text(to, body):
    sent_texts.append(body)


async def fake_send_buttons(to, body, buttons):
    sent_buttons_calls.append({"body": body, "buttons": buttons})


download_calls = []


async def fake_download_media(media_id):
    download_calls.append(media_id)
    return REFERENCE_PHOTO_BYTES


wa_client.send_text = fake_send_text
wa_client.send_buttons = fake_send_buttons
wa_client.download_media = fake_download_media
router.send_text = fake_send_text
image_intent.send_text = fake_send_text
image_intent.send_buttons = fake_send_buttons
image_intent.download_media = fake_download_media


async def fake_router_classify(text):
    if not text or not text.strip():
        return {"intent": "OTHER", "command": None}
    return router_intent._fallback_classify(text)


router_intent.classify = fake_router_classify

reference_upload_calls = []


def fake_upload_reference_image(business_id, image_bytes):
    url = f"https://fake.example.com/references/{business_id}/{len(reference_upload_calls)}.png"
    reference_upload_calls.append(url)
    return url


image_intent.upload_reference_image = fake_upload_reference_image

from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile, ConversationState  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402
from app.credits import add_credits  # noqa: E402


def _make_business(phone):
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="restaurant", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        db.add(BrandProfile(business_id=biz_id, tone="bold"))
        add_credits(db, biz_id, 20, reason="signup_bonus")
        return biz_id


def _pending(biz_id):
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == biz_id).first()
        return convo.pending_image_intent if convo else None


class _FakeContent:
    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeContent(text)]


async def run():
    ctx = BusinessContext(name="Test Bakery", industry="bakery", tone="playful")

    print("=" * 60)
    print("TEST 1: _understand_image() parses an EDIT_CANDIDATE classification")
    print("=" * 60)

    async def fake_create_message_edit_candidate(**kwargs):
        return _FakeResponse('{"category": "EDIT_CANDIDATE", "response": ""}')

    image_intent.create_message = fake_create_message_edit_candidate
    result = await image_intent._understand_image(ctx, png_bytes())
    assert result == {"category": "EDIT_CANDIDATE", "response": None}
    print(f"PASS: {result}\n")

    print("=" * 60)
    print("TEST 2: _understand_image() parses an INFORMATIVE classification with a real response")
    print("=" * 60)

    async def fake_create_message_informative(**kwargs):
        return _FakeResponse(
            '{"category": "INFORMATIVE", "response": "This looks like a competitor'"'"'s festive offer post — '
            'they'"'"'re leading with 30% off, which is aggressive for this category. You could differentiate '
            'on quality/freshness messaging instead of matching the discount."}'
        )

    image_intent.create_message = fake_create_message_informative
    result = await image_intent._understand_image(ctx, png_bytes())
    assert result["category"] == "INFORMATIVE"
    assert result["response"] and "competitor" in result["response"].lower()
    print(f"PASS: {result}\n")

    print("=" * 60)
    print("TEST 3: _understand_image() parses an AMBIGUOUS classification")
    print("=" * 60)

    async def fake_create_message_ambiguous(**kwargs):
        return _FakeResponse('{"category": "AMBIGUOUS", "response": ""}')

    image_intent.create_message = fake_create_message_ambiguous
    result = await image_intent._understand_image(ctx, png_bytes())
    assert result == {"category": "AMBIGUOUS", "response": None}
    print(f"PASS: {result}\n")

    print("=" * 60)
    print("TEST 4: _understand_image() fails safe to AMBIGUOUS/None on a malformed response")
    print("=" * 60)

    async def fake_create_message_garbage(**kwargs):
        return _FakeResponse("not JSON at all")

    image_intent.create_message = fake_create_message_garbage
    result = await image_intent._understand_image(ctx, png_bytes())
    assert result == {"category": "AMBIGUOUS", "response": None}
    print(f"PASS: {result}\n")

    print("=" * 60)
    print("TEST 5: _understand_image() fails safe to AMBIGUOUS/None on an unexpected category value")
    print("=" * 60)

    async def fake_create_message_bad_category(**kwargs):
        return _FakeResponse('{"category": "SOMETHING_MADE_UP", "response": ""}')

    image_intent.create_message = fake_create_message_bad_category
    result = await image_intent._understand_image(ctx, png_bytes())
    assert result == {"category": "AMBIGUOUS", "response": None}
    print(f"PASS: {result}\n")

    print("=" * 60)
    print("TEST 6: start() with an INFORMATIVE image sends the response directly, no button menu")
    print("=" * 60)
    phone1 = "919999998810"
    biz_id1 = _make_business(phone1)
    sent_texts.clear()
    sent_buttons_calls.clear()
    download_calls.clear()

    async def fake_create_message_menu_flyer(**kwargs):
        return _FakeResponse(
            '{"category": "INFORMATIVE", "response": "This looks like your menu — the pricing tier for '
            'desserts stands out as your highest-margin category, worth featuring more."}'
        )

    image_intent.create_message = fake_create_message_menu_flyer

    await router._process_message(biz_id1, IncomingMessage(sender=phone1, type="image", media_id="media-flyer", text=None))

    assert sent_buttons_calls == [], f"FAIL: an informative image should never trigger the edit-menu, got {sent_buttons_calls}"
    assert any("menu" in t.lower() and "dessert" in t.lower() for t in sent_texts), (
        f"FAIL: expected the vision-grounded response sent as text, got {sent_texts}"
    )
    assert _pending(biz_id1) is None, "FAIL: an informative image should not start a pending edit negotiation"
    assert download_calls == ["media-flyer"], f"FAIL: expected the image downloaded exactly once, got {download_calls}"
    print(f"PASS: {sent_texts[-1]!r}\n")

    print("=" * 60)
    print("TEST 7: start() with an EDIT_CANDIDATE image still falls through to the existing button menu")
    print("=" * 60)
    phone2 = "919999998811"
    biz_id2 = _make_business(phone2)
    sent_texts.clear()
    sent_buttons_calls.clear()

    image_intent.create_message = fake_create_message_edit_candidate

    await router._process_message(biz_id2, IncomingMessage(sender=phone2, type="image", media_id="media-product", text=None))

    assert len(sent_buttons_calls) == 1, f"FAIL: expected the edit-menu for a product photo, got {sent_buttons_calls}"
    assert _pending(biz_id2) is not None
    print("PASS: edit-candidate still gets the button menu, unchanged\n")

    print("=" * 60)
    print("TEST 8: start() with an AMBIGUOUS/failed classification falls back to the existing button menu")
    print("=" * 60)
    phone3 = "919999998812"
    biz_id3 = _make_business(phone3)
    sent_buttons_calls.clear()

    async def fake_create_message_raises(**kwargs):
        raise RuntimeError("simulated vision failure")

    image_intent.create_message = fake_create_message_raises

    await router._process_message(biz_id3, IncomingMessage(sender=phone3, type="image", media_id="media-unclear", text=None))

    assert len(sent_buttons_calls) == 1, f"FAIL: a vision-call failure must fail safe to the existing menu, got {sent_buttons_calls}"
    assert _pending(biz_id3) is not None
    print("PASS: a vision failure falls back to the existing menu, nothing left unhandled\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
