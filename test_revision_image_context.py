"""
Test for the "image edit context not persisting" bug in
app/engine/orchestrator.py's _run_generation().

Previously, a revision request (e.g. "change background to black") only
ever read the parent Generation's built_prompt (TEXT) to build a new
prompt -- it never fetched the parent's actual image bytes, so the image
model always generated a brand-new, unrelated image from a re-described
text prompt instead of actually editing the specific image the client was
looking at. This adds: fetching parent.base_image_url (falling back to
parent.image_url) via HTTP and passing those bytes through to
image_gen.generate_images() as reference_image, whenever the incoming
message itself didn't already attach a fresher photo.

Also covers the related "bot goes silent" fix: the whole
"give me a moment" / "Creating your design..." send, and the revision
prompt-building + parent-image-fetch block, now all run INSIDE
_run_generation()'s try/except -- a failure anywhere in there must reach
the user as the standard "something went wrong" message, never escape
uncaught and leave the client with no reply at all.

Covers:
  - A revision with no attached photo -> the parent's base_image_url is
    fetched and passed to image_gen as reference_image.
  - A revision where the message ALREADY has an attached photo -> that
    takes priority, the parent is never fetched at all.
  - The parent image fetch returning a non-200 status -> falls back to a
    text-only revision (reference_image stays None) instead of raising or
    hanging.
  - The parent image fetch raising an exception (network error) -> same
    graceful fallback, generation still completes and something is
    delivered.
  - A failure in the very first thing _run_generation() does (the
    first-ever-generation reflect_first_result() send) is caught by the
    surrounding try/except and reaches the client as the standard failure
    message, instead of silently going uncaught.
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_revision_image_context.db"
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


def png_bytes(color=(200, 50, 50), size=(64, 64)):
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


from app.whatsapp import client as wa_client  # noqa: E402

sent_texts, sent_images = [], []


async def fake_send_text(to, body):
    sent_texts.append(body)


async def fake_send_image(to, image_url, caption=""):
    sent_images.append(image_url)


async def fake_send_image_with_button(to, image_url, body, button_id, button_label):
    sent_images.append(image_url)


wa_client.send_text = fake_send_text
wa_client.send_image = fake_send_image
wa_client.send_image_with_button = fake_send_image_with_button

from app.engine import orchestrator as orch  # noqa: E402

async def _fake_content_policy_check(text):
    return {"allowed": True, "reason": None}

orch.content_policy.check = _fake_content_policy_check
orch.send_text = fake_send_text
orch.send_image = fake_send_image
orch.send_image_with_button = fake_send_image_with_button

from app.engine import prompt_builder  # noqa: E402


async def fake_build(ctx, user_brief):
    return {"image_prompt": f"prompt: {user_brief}", "headline_text": "Sale", "notes_for_caption": user_brief}


prompt_builder.build = fake_build
orch.prompt_builder.build = fake_build

from app.engine import image_gen  # noqa: E402

image_gen_calls = []


async def fake_generate_images(prompt, count=2, reference_image=None):
    image_gen_calls.append({"prompt": prompt, "reference_image": reference_image})
    return [png_bytes(), png_bytes()]


image_gen.generate_images = fake_generate_images
orch.image_gen.generate_images = fake_generate_images

from app.engine import quality  # noqa: E402


async def fake_score_and_pick(images):
    return {"best_index": 0, "best_score": 90, "issues": []}


quality.score_and_pick = fake_score_and_pick
orch.quality.score_and_pick = fake_score_and_pick

from app.engine import caption as caption_engine  # noqa: E402


async def fake_caption_generate(ctx, notes_for_caption):
    return {"caption": "Great offer!", "hashtags": "#offer"}


caption_engine.generate = fake_caption_generate
orch.caption_engine.generate = fake_caption_generate


def fake_upload_creative(business_id, generation_id, image_bytes):
    return f"https://fake.example.com/creatives/{generation_id}.png"


def fake_upload_base_image(business_id, generation_id, image_bytes):
    return f"https://fake.example.com/creatives/{generation_id}_base.png"


orch.upload_creative = fake_upload_creative
orch.upload_base_image = fake_upload_base_image

from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile, ConversationState, Generation  # noqa: E402
from app.engine.context import BusinessContext  # noqa: E402
from app.credits import add_credits  # noqa: E402


def _make_business_with_parent(phone, parent_base_image_url="https://fake.example.com/creatives/parent_base.png"):
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="restaurant", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        db.add(BrandProfile(business_id=biz_id, tone="bold"))
        add_credits(db, biz_id, 20, reason="signup_bonus")
        db.add(ConversationState(business_id=biz_id))

        parent = Generation(
            business_id=biz_id,
            user_message="Create a Diwali offer post",
            built_prompt="A festive Diwali offer creative with diyas and gold tones",
            image_url="https://fake.example.com/creatives/parent_final.png",
            base_image_url=parent_base_image_url,
            status="done",
            quality_score=90,
            credits_charged=1,
        )
        db.add(parent)
        db.flush()
        parent_id = parent.id
        db.commit()
        return biz_id, parent_id


def _ctx():
    return BusinessContext(name="Test Biz", industry="restaurant", tone="bold")


class _FakeResponse:
    def __init__(self, status_code, content):
        self.status_code = status_code
        self.content = content


class _FakeHttpClient:
    """Stand-in for httpx.AsyncClient, used as an async context manager."""
    def __init__(self, response=None, raise_exc=None, calls=None):
        self._response = response
        self._raise_exc = raise_exc
        self._calls = calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        self._calls.append(url)
        if self._raise_exc:
            raise self._raise_exc
        return self._response


def _install_fake_httpx(response=None, raise_exc=None):
    calls = []

    def factory(*args, **kwargs):
        return _FakeHttpClient(response=response, raise_exc=raise_exc, calls=calls)

    orch.httpx.AsyncClient = factory
    return calls


async def run():
    print("=" * 60)
    print("TEST 1: revision, no attached photo -> parent's base_image_url is fetched and used as reference_image")
    print("=" * 60)
    phone = "919999999940"
    biz_id, parent_id = _make_business_with_parent(phone)
    image_gen_calls.clear()
    sent_texts.clear()

    fetch_calls = _install_fake_httpx(response=_FakeResponse(200, b"PARENT-IMAGE-BYTES"))

    await orch._run_generation(
        biz_id, phone, _ctx(), "change background to black", "change background to black",
        last_generation_id=parent_id, is_revision=True, trigger_source="revision",
    )

    assert fetch_calls == ["https://fake.example.com/creatives/parent_base.png"], (
        f"FAIL: expected the parent's base_image_url fetched, got {fetch_calls}"
    )
    assert len(image_gen_calls) == 1, f"FAIL: expected exactly one image_gen call, got {image_gen_calls}"
    assert image_gen_calls[0]["reference_image"] == b"PARENT-IMAGE-BYTES", (
        f"FAIL: expected the parent's actual image bytes used as the edit reference, got {image_gen_calls[0]}"
    )
    assert any("Here's your creative" in t for t in sent_texts), f"FAIL: expected a successful delivery message, got {sent_texts}"
    print("PASS: revision fetched and used the parent's actual image as the edit reference\n")

    print("=" * 60)
    print("TEST 2: revision WITH an attached photo -> that photo wins, parent is never fetched")
    print("=" * 60)
    image_gen_calls.clear()
    sent_texts.clear()
    fetch_calls = _install_fake_httpx(response=_FakeResponse(200, b"PARENT-IMAGE-BYTES"))

    await orch._run_generation(
        biz_id, phone, _ctx(), "change background to black", "change background to black",
        last_generation_id=parent_id, is_revision=True, trigger_source="revision",
        reference_image=b"FRESHLY-ATTACHED-PHOTO",
    )

    assert fetch_calls == [], f"FAIL: expected the parent image NOT to be fetched when a fresh photo was already attached, got {fetch_calls}"
    assert image_gen_calls[0]["reference_image"] == b"FRESHLY-ATTACHED-PHOTO", (
        f"FAIL: expected the freshly attached photo to take priority, got {image_gen_calls[0]}"
    )
    print("PASS: an already-attached photo takes priority, parent fetch skipped entirely\n")

    print("=" * 60)
    print("TEST 3: parent image fetch returns non-200 -> falls back to text-only revision, no crash")
    print("=" * 60)
    image_gen_calls.clear()
    sent_texts.clear()
    _install_fake_httpx(response=_FakeResponse(404, b""))

    await orch._run_generation(
        biz_id, phone, _ctx(), "change background to black", "change background to black",
        last_generation_id=parent_id, is_revision=True, trigger_source="revision",
    )

    assert len(image_gen_calls) == 1, f"FAIL: expected generation to still proceed, got {image_gen_calls}"
    assert image_gen_calls[0]["reference_image"] is None, (
        f"FAIL: expected a graceful fallback to no reference image, got {image_gen_calls[0]}"
    )
    assert any("Here's your creative" in t for t in sent_texts), f"FAIL: expected delivery to still succeed despite the failed fetch, got {sent_texts}"
    print("PASS: a failed parent-image fetch falls back cleanly, generation still completes\n")

    print("=" * 60)
    print("TEST 4: parent image fetch raises (network error) -> same graceful fallback, no hang/crash")
    print("=" * 60)
    image_gen_calls.clear()
    sent_texts.clear()
    _install_fake_httpx(raise_exc=RuntimeError("simulated network failure"))

    await orch._run_generation(
        biz_id, phone, _ctx(), "change background to black", "change background to black",
        last_generation_id=parent_id, is_revision=True, trigger_source="revision",
    )

    assert len(image_gen_calls) == 1, f"FAIL: expected generation to still proceed despite the fetch exception, got {image_gen_calls}"
    assert image_gen_calls[0]["reference_image"] is None
    assert any("Here's your creative" in t for t in sent_texts), f"FAIL: expected delivery to still succeed, got {sent_texts}"
    print("PASS: an exception fetching the parent image is caught, generation still completes -- never a silent hang\n")

    print("=" * 60)
    print("TEST 5: a failure in the FIRST thing _run_generation() does (first-generation reflect message) is caught, not left uncaught")
    print("=" * 60)
    from app.engine import brand_reflection

    async def fake_reflect_first_result_fails(ctx):
        raise RuntimeError("simulated Claude failure composing the reflect message")

    real_reflect = brand_reflection.reflect_first_result
    brand_reflection.reflect_first_result = fake_reflect_first_result_fails
    orch.brand_reflection.reflect_first_result = fake_reflect_first_result_fails

    phone2 = "919999999941"
    with get_session() as db:
        biz2 = Business(phone=phone2, name="Test Biz 2", industry="salon", onboarding_state="done")
        db.add(biz2)
        db.flush()
        biz2_id = biz2.id
        db.add(BrandProfile(business_id=biz2_id))
        add_credits(db, biz2_id, 20, reason="signup_bonus")
        db.add(ConversationState(business_id=biz2_id))

    sent_texts.clear()
    await orch._run_generation(
        biz2_id, phone2, _ctx(), "Create my first post", "Create my first post",
        last_generation_id=None, is_revision=False, trigger_source="onboarding_complete",
    )

    assert sent_texts, "FAIL: expected the client to receive SOME reply even though reflect_first_result() raised"
    assert any("went wrong" in t.lower() for t in sent_texts), (
        f"FAIL: expected the standard failure message once the exception was caught, got {sent_texts}"
    )
    print(f"PASS: the client still got a reply instead of silence: {sent_texts}\n")

    brand_reflection.reflect_first_result = real_reflect
    orch.brand_reflection.reflect_first_result = real_reflect

    print("ALL TESTS PASSED")


asyncio.run(run())
