"""
Test for the "uploaded photo dropped during a proposal negotiation" fix in
app/engine/orchestrator.py's generate().

Root cause from the Aug 2026 live-test report, item 2: a photo attached to
a request vague enough to need a concept proposal first (e.g. "use this
for a post" + an attached product photo) was downloaded but then simply
discarded -- concept_proposal.decide() returning NEEDS_PROPOSAL meant the
downloaded bytes were never threaded anywhere, and even once the client
later confirmed (a plain "yes", no photo attached to that reply), the
original photo was never revisited. Fixed by persisting the photo to R2
the moment a proposal negotiation starts (or gets a fresh photo on any
ADJUST turn), storing the URL on pending_proposal, and re-fetching it
whenever generation actually happens (CONFIRM, or the ADJUST-round cap).

Covers:
  - A photo attached to the message that triggers NEEDS_PROPOSAL is
    persisted immediately, and the URL is stored on pending_proposal.
  - Confirming with a later plain-text "yes" (no photo on that message)
    still uses the ORIGINALLY uploaded photo as the reference image.
  - An ADJUST round carries the stored reference forward to the next
    pending_proposal, and hitting the ADJUST-round cap still uses it.
  - A FRESH photo attached to a later ADJUST/CONFIRM reply takes priority
    over the one stored from an earlier turn.
"""
import sys
import asyncio
import os
import io
import json

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_proposal_reference_image.db"
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


def png_bytes(color, size=(64, 64)):
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


ORIGINAL_PHOTO = png_bytes((10, 20, 30))
FRESH_PHOTO = png_bytes((200, 200, 200))

from app.whatsapp import client as wa_client  # noqa: E402

sent_texts = []


async def fake_send_text(to, body):
    sent_texts.append(body)


download_media_calls = []


async def fake_download_media(media_id):
    download_media_calls.append(media_id)
    return ORIGINAL_PHOTO if media_id == "media-original" else FRESH_PHOTO


wa_client.send_text = fake_send_text
wa_client.download_media = fake_download_media

from app.engine import orchestrator as orch  # noqa: E402
orch.send_text = fake_send_text
orch.download_media = fake_download_media

from app.engine import intent as intent_engine  # noqa: E402


async def fake_classify(user_message):
    return {"intent": "GENERATE", "brief": user_message}


intent_engine.classify = fake_classify
orch.intent_engine.classify = fake_classify

from app.engine import concept_proposal  # noqa: E402


async def fake_decide(ctx, user_message):
    return {
        "decision": "NEEDS_PROPOSAL",
        "proposal_text": "How about a clean product-focused layout with your logo bottom-right?",
        "concept_brief": "round0: use the uploaded photo as the subject",
    }


concept_proposal.decide = fake_decide
orch.concept_proposal.decide = fake_decide

adjust_counter = {"n": 0}


async def fake_interpret_reply(ctx, previous_proposal, client_reply):
    if client_reply.strip().lower() in ("yes", "go ahead", "looks good"):
        return {"classification": "CONFIRM"}
    adjust_counter["n"] += 1
    n = adjust_counter["n"]
    return {
        "classification": "ADJUST",
        "proposal_text": f"Round {n} revised proposal — how about this instead?",
        "concept_brief": f"round{n}: revised concept",
    }


concept_proposal.interpret_reply = fake_interpret_reply
orch.concept_proposal.interpret_reply = fake_interpret_reply

reference_upload_calls = []


def fake_upload_reference_image(business_id, image_bytes):
    url = f"https://fake.example.com/references/{business_id}/{len(reference_upload_calls)}.png"
    reference_upload_calls.append((url, image_bytes))
    return url


orch.upload_reference_image = fake_upload_reference_image

run_generation_calls = []


async def fake_run_generation(business_id, phone, ctx, brief, user_message, last_generation_id, is_revision, trigger_source=None, reference_image=None):
    run_generation_calls.append({"brief": brief, "trigger_source": trigger_source, "reference_image": reference_image})


orch._run_generation = fake_run_generation


# generate() re-fetches the stored reference image over HTTP -- fake that
# fetch, keyed by URL, returning whichever bytes were uploaded under it.


class _FakeRefResponse:
    def __init__(self, content):
        self.status_code = 200
        self.content = content


class _FakeRefHttpClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        for stored_url, content in reference_upload_calls:
            if stored_url == url:
                return _FakeRefResponse(content)
        return _FakeRefResponse(b"")


orch.httpx.AsyncClient = lambda *a, **kw: _FakeRefHttpClient()

from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile, ConversationState  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402
from app.engine.orchestrator import generate  # noqa: E402
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
        return json.loads(convo.pending_proposal) if convo and convo.pending_proposal else None


async def run():
    print("=" * 60)
    print("TEST 1: a photo attached to the NEEDS_PROPOSAL-triggering message is persisted, URL stored on pending_proposal")
    print("=" * 60)
    phone = "919999999990"
    biz_id = _make_business(phone)
    reference_upload_calls.clear()

    await generate(biz_id, IncomingMessage(
        sender=phone, type="image", media_id="media-original", text="use this for a post",
    ))

    assert len(reference_upload_calls) == 1, f"FAIL: expected the photo persisted immediately, got {reference_upload_calls}"
    pending = _pending(biz_id)
    assert pending is not None and pending.get("reference_image_url") == reference_upload_calls[0][0], (
        f"FAIL: expected the reference URL stored on pending_proposal, got {pending}"
    )
    print("PASS: photo persisted at proposal time, URL stored on pending_proposal\n")

    print("=" * 60)
    print("TEST 2: confirming later with plain text (no photo attached) still uses the ORIGINAL uploaded photo")
    print("=" * 60)
    run_generation_calls.clear()
    await generate(biz_id, IncomingMessage(sender=phone, type="text", text="yes"))

    assert len(run_generation_calls) == 1, f"FAIL: expected exactly one generation call, got {run_generation_calls}"
    assert run_generation_calls[0]["reference_image"] == ORIGINAL_PHOTO, (
        "FAIL: expected the originally uploaded photo used as the reference image on confirm"
    )
    assert run_generation_calls[0]["trigger_source"] == "proposal_confirmed"
    assert _pending(biz_id) is None, "FAIL: expected pending_proposal cleared after confirming"
    print("PASS: confirm reused the originally uploaded photo despite no photo on the confirm message itself\n")

    print("=" * 60)
    print("TEST 3: an ADJUST round carries the stored reference forward; hitting the ADJUST-cap still uses it")
    print("=" * 60)
    phone2 = "919999999991"
    biz_id2 = _make_business(phone2)
    reference_upload_calls.clear()
    adjust_counter["n"] = 0
    run_generation_calls.clear()

    await generate(biz_id2, IncomingMessage(sender=phone2, type="image", media_id="media-original", text="use this photo for something"))
    pending2 = _pending(biz_id2)
    stored_url = pending2["reference_image_url"]

    # Round 1: ADJUST, no photo attached this turn -- reference should carry forward unchanged.
    await generate(biz_id2, IncomingMessage(sender=phone2, type="text", text="make it more colorful"))
    pending2 = _pending(biz_id2)
    assert pending2["reference_image_url"] == stored_url, f"FAIL: expected the reference URL to carry forward through ADJUST, got {pending2}"

    # Round 2: ADJUST again.
    await generate(biz_id2, IncomingMessage(sender=phone2, type="text", text="actually less text"))

    # Round 3: would be ADJUST again -> hits the cap -> generates using the stored reference.
    await generate(biz_id2, IncomingMessage(sender=phone2, type="text", text="one more tweak"))

    assert len(run_generation_calls) == 1, f"FAIL: expected the ADJUST-cap to trigger exactly one generation, got {run_generation_calls}"
    assert run_generation_calls[0]["trigger_source"] == "adjust_cap"
    assert run_generation_calls[0]["reference_image"] == ORIGINAL_PHOTO, (
        "FAIL: expected the ADJUST-cap generation to still use the originally uploaded photo"
    )
    print("PASS: reference image survived multiple ADJUST rounds and was used when the cap triggered generation\n")

    print("=" * 60)
    print("TEST 4: a FRESH photo attached to a later ADJUST reply takes priority over the stored one")
    print("=" * 60)
    phone3 = "919999999992"
    biz_id3 = _make_business(phone3)
    reference_upload_calls.clear()
    adjust_counter["n"] = 0
    run_generation_calls.clear()

    await generate(biz_id3, IncomingMessage(sender=phone3, type="image", media_id="media-original", text="use this for a post"))
    # A fresh photo attached to the ADJUST reply itself.
    await generate(biz_id3, IncomingMessage(sender=phone3, type="image", media_id="media-fresh", text="actually use this one instead"))
    pending3 = _pending(biz_id3)
    fresh_url = pending3["reference_image_url"]
    assert fresh_url != reference_upload_calls[0][0], "FAIL: expected a NEW reference URL for the freshly attached photo"

    await generate(biz_id3, IncomingMessage(sender=phone3, type="text", text="yes"))
    assert run_generation_calls[-1]["reference_image"] == FRESH_PHOTO, (
        "FAIL: expected the freshly attached photo (not the original) to be used once confirmed"
    )
    print("PASS: the fresher photo took priority over the one stored from the opening message\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
