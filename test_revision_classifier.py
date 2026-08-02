"""
Smoke test for the revision classifier + free logo-move fast path. Claude,
image gen, WhatsApp, and R2 are all mocked so this tests the CONTROL FLOW:

  1. A logo-position revision recomposites the parent's stored base image at
     the new position, charges 0 credits, and never touches the expensive
     pipeline (prompt build / image gen / quality check).
  2. A real creative-change revision still runs the full pipeline and charges
     1 credit as before.
  3. A logo-position revision on a parent with NO base_image_url falls back
     safely to full regeneration (and charges normally).

Unlike the other smoke tests this one runs against real Postgres by default
(run `alembic upgrade head` first, or let init_db create the tables) —
override with TEST_DATABASE_URL to use something else, e.g. sqlite.
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")

DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://socioburp:socioburp@localhost:5432/socioburp_test")
os.environ["DATABASE_URL"] = DB_URL
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

RED = (200, 50, 50)
BLUE = (30, 60, 220)


def png_bytes(color, size=(1024, 1024)):
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def pixel(image_bytes, xy):
    return Image.open(io.BytesIO(image_bytes)).convert("RGB").getpixel(xy)


LOGO_BYTES = png_bytes(BLUE, size=(200, 200))

# --- Mock WhatsApp sends ---
from app.whatsapp import client as wa_client  # noqa: E402

sent_texts, sent_images = [], []


async def fake_send_text(to, body):
    sent_texts.append(body)
    print(f"[SEND TEXT]: {body}\n")


async def fake_send_image(to, image_url, caption=""):
    sent_images.append(image_url)
    print(f"[SEND IMAGE]: {image_url}\n")


wa_client.send_text = fake_send_text
wa_client.send_image = fake_send_image

from app.engine import orchestrator  # noqa: E402
orchestrator.send_text = fake_send_text
orchestrator.send_image = fake_send_image

# --- Count every expensive pipeline call ---
calls = {"prompt_build": 0, "image_gen": 0, "quality": 0, "caption": 0}

from app.engine import intent as intent_engine  # noqa: E402


async def fake_classify(user_message):
    text = user_message.lower()
    if "create" in text:
        return {"intent": "GENERATE", "brief": user_message}
    return {"intent": "REVISE", "brief": user_message}


intent_engine.classify = fake_classify
orchestrator.intent_engine.classify = fake_classify

from app.engine import concept_proposal  # noqa: E402


async def fake_decide(ctx, user_message):
    return {"decision": "SPECIFIC_ENOUGH", "brief": user_message}


concept_proposal.decide = fake_decide
orchestrator.concept_proposal.decide = fake_decide

from app.engine import revision_classifier  # noqa: E402


async def fake_rev_classify(user_message):
    text = user_message.lower()
    if "logo" in text and "top left" in text:
        return {"revision_type": "LOGO_POSITION", "position": "top-left", "brief": user_message}
    if "logo" in text and "bottom left" in text:
        return {"revision_type": "LOGO_POSITION", "position": "bottom-left", "brief": user_message}
    return {"revision_type": "FULL_REGENERATION", "brief": user_message}


revision_classifier.classify = fake_rev_classify
orchestrator.revision_classifier.classify = fake_rev_classify

from app.engine import prompt_builder  # noqa: E402


async def fake_build(ctx, user_brief):
    calls["prompt_build"] += 1
    return {"image_prompt": f"creative for {ctx.name}: {user_brief}", "notes_for_caption": user_brief}


prompt_builder.build = fake_build
orchestrator.prompt_builder.build = fake_build

from app.engine import image_gen  # noqa: E402


async def fake_generate_images(prompt, count=2):
    calls["image_gen"] += 1
    return [png_bytes(RED), png_bytes(RED)]


image_gen.generate_images = fake_generate_images
orchestrator.image_gen.generate_images = fake_generate_images

from app.engine import quality  # noqa: E402


async def fake_score_and_pick(images):
    calls["quality"] += 1
    return {"best_index": 0, "best_score": 82, "issues": []}


quality.score_and_pick = fake_score_and_pick
orchestrator.quality.score_and_pick = fake_score_and_pick

from app.engine import caption as caption_engine  # noqa: E402


async def fake_caption_generate(ctx, notes_for_caption):
    calls["caption"] += 1
    return {"caption": f"Check this out! {notes_for_caption}", "hashtags": "#offer #socioburp"}


caption_engine.generate = fake_caption_generate
orchestrator.caption_engine.generate = fake_caption_generate

# --- Mock R2: uploads record their bytes, downloads serve them back ---
uploaded = {}  # url -> bytes


def fake_upload_creative(business_id, generation_id, image_bytes):
    url = f"https://fake-cdn.example.com/creatives/{business_id}/{generation_id}.png"
    uploaded[url] = image_bytes
    return url


def fake_upload_base_image(business_id, generation_id, image_bytes):
    url = f"https://fake-cdn.example.com/creatives/{business_id}/{generation_id}_base.png"
    uploaded[url] = image_bytes
    return url


orchestrator.upload_creative = fake_upload_creative
orchestrator.upload_base_image = fake_upload_base_image


# --- Mock httpx inside the orchestrator (logo + base image fetches) ---
class FakeResponse:
    def __init__(self, content):
        self.status_code = 200
        self.content = content


class FakeAsyncClient:
    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url):
        if url in uploaded:
            return FakeResponse(uploaded[url])
        return FakeResponse(LOGO_BYTES)  # the brand logo


class FakeHttpx:
    AsyncClient = FakeAsyncClient


orchestrator.httpx = FakeHttpx()

from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile, Generation, ConversationState, CreditLedger  # noqa: E402
from app.credits import add_credits, get_balance  # noqa: E402
from app.engine.orchestrator import generate  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402


async def run():
    phone = "919999999999"

    # Clean slate so the test is repeatable against a persistent Postgres
    with get_session() as db:
        for model in (CreditLedger, ConversationState, Generation, BrandProfile, Business):
            db.query(model).delete()

    with get_session() as db:
        biz = Business(phone=phone, name="Copper & Crumb", industry="restaurant", onboarding_state="done")
        db.add(biz)
        db.flush()
        business_id = biz.id
        db.add(BrandProfile(
            business_id=business_id, tone="premium",
            logo_url="https://fake-cdn.example.com/logos/copper-crumb.png",
        ))
        add_credits(db, business_id, 20, reason="signup_bonus")

    print("=" * 60)
    print("SETUP: fresh generation -> parent with base_image_url stored")
    print("=" * 60)
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="Create a weekend offer post, 20% off"))
    with get_session() as db:
        parent = db.query(Generation).filter(Generation.business_id == business_id).first()
        assert parent.status == "done", f"FAIL: parent status {parent.status}"
        assert parent.base_image_url is not None, "FAIL: pipeline did not store base_image_url!"
        parent_id = parent.id
        parent_base_url = parent.base_image_url
    assert get_balance(business_id) == 19, f"FAIL: expected 19 credits, got {get_balance(business_id)}"
    # Parent creative: logo composited at the default bottom-right
    parent_img = uploaded[sent_images[-1]]
    assert pixel(parent_img, (900, 900)) != RED, "FAIL: parent should have logo at bottom-right"
    assert pixel(parent_img, (30, 30)) == RED, "FAIL: parent top-left should be plain background"
    print(f"PASS: parent generation done, base stored, balance 19, calls={calls}\n")

    print("=" * 60)
    print("TEST 1: logo-position revision -> recomposite, FREE, no pipeline")
    print("=" * 60)
    calls_before = dict(calls)
    sent_images.clear()
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="move the logo to the top left"))
    assert calls == calls_before, f"FAIL: expensive pipeline ran! before={calls_before} after={calls}"
    assert get_balance(business_id) == 19, f"FAIL: logo move charged credits! balance={get_balance(business_id)}"
    with get_session() as db:
        child = (
            db.query(Generation)
            .filter(Generation.business_id == business_id, Generation.parent_id == parent_id)
            .first()
        )
        assert child is not None and child.status == "done", "FAIL: no completed child generation row!"
        assert child.credits_charged == 0, f"FAIL: credits_charged={child.credits_charged}, expected 0"
        assert child.base_image_url == parent_base_url, "FAIL: base_image_url not carried to the child!"
        child_id = child.id
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        assert convo.last_generation_id == child_id, "FAIL: last_generation_id not updated!"
    moved_img = uploaded[sent_images[-1]]
    assert pixel(moved_img, (30, 30)) != RED, "FAIL: logo not composited at top-left!"
    assert pixel(moved_img, (900, 900)) == RED, "FAIL: bottom-right should be plain background after move!"
    print("PASS: recomposited at top-left, 0 credits, pipeline untouched\n")

    print("=" * 60)
    print("TEST 2: real creative revision -> full pipeline, charged normally")
    print("=" * 60)
    calls_before = dict(calls)
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="make it more premium"))
    assert calls["prompt_build"] == calls_before["prompt_build"] + 1, "FAIL: prompt builder did not run!"
    assert calls["image_gen"] > calls_before["image_gen"], "FAIL: image gen did not run!"
    assert calls["caption"] == calls_before["caption"] + 1, "FAIL: caption did not run!"
    assert get_balance(business_id) == 18, f"FAIL: expected 18 credits, got {get_balance(business_id)}"
    with get_session() as db:
        rev = (
            db.query(Generation)
            .filter(Generation.business_id == business_id, Generation.parent_id == child_id)
            .first()
        )
        assert rev is not None and rev.status == "done", "FAIL: revision generation missing!"
        assert rev.credits_charged == 1, f"FAIL: credits_charged={rev.credits_charged}, expected 1"
        rev_id = rev.id
    print("PASS: full pipeline ran, 1 credit charged\n")

    print("=" * 60)
    print("TEST 3: logo move but parent has NO base_image_url -> safe fallback")
    print("=" * 60)
    with get_session() as db:
        rev = db.query(Generation).filter(Generation.id == rev_id).first()
        rev.base_image_url = None  # simulate a pre-migration row / failed base upload
    calls_before = dict(calls)
    await generate(business_id, IncomingMessage(sender=phone, type="text", text="put the logo bottom left"))
    assert calls["prompt_build"] == calls_before["prompt_build"] + 1, "FAIL: fallback did not run the pipeline!"
    assert get_balance(business_id) == 17, f"FAIL: expected 17 credits, got {get_balance(business_id)}"
    with get_session() as db:
        fb = (
            db.query(Generation)
            .filter(Generation.business_id == business_id, Generation.parent_id == rev_id)
            .first()
        )
        assert fb is not None and fb.status == "done", "FAIL: fallback generation missing!"
        assert fb.credits_charged == 1, f"FAIL: fallback credits_charged={fb.credits_charged}, expected 1"
    print("PASS: fell back to full regeneration, charged normally\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
