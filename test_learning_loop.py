"""
Tests for the accept-triggered learning loop (app/engine/learning.py).

Part A: direct unit tests of record_accepted_direction / get_learned_preferences
  - storage, dedup-moves-to-most-recent, cap at MAX_LEARNED_PREFERENCES

Part B: integration test through the real orchestrator.generate(), proving
the two trigger rules actually hold:
  - GENERATE -> GENERATE (no revision in between) DOES record the first
    generation's request as accepted
  - GENERATE -> REVISE does NOT record anything (a revision is a reject)
  - GENERATE -> REVISE -> GENERATE records the REVISION's request (the
    final, most-recent state of that chain is what was actually accepted)
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_learning.db"
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


from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile, Generation, LearningEvent  # noqa: E402
from app.engine import learning  # noqa: E402


def make_generation(business_id, user_message, quality_score=80):
    with get_session() as db:
        gen = Generation(business_id=business_id, user_message=user_message, status="done", quality_score=quality_score)
        db.add(gen)
        db.flush()
        return gen.id


def get_learned(business_id):
    with get_session() as db:
        p = db.query(BrandProfile).filter(BrandProfile.business_id == business_id).first()
        return list((p.extras or {}).get("learned_preferences", []))


print("=" * 60)
print("PART A: direct unit tests of learning.py")
print("=" * 60)


async def part_a():
    with get_session() as db:
        biz_a = Business(phone="919999999996", name="Unit Test Biz", industry="bakery", onboarding_state="done")
        db.add(biz_a)
        db.flush()
        biz_a_id = biz_a.id
        db.add(BrandProfile(business_id=biz_a_id, tone="premium"))

    gen1 = make_generation(biz_a_id, "Create weekend offer post")
    await learning.record_accepted_direction(biz_a_id, gen1)
    assert get_learned(biz_a_id) == ["Create weekend offer post"], f"FAIL: {get_learned(biz_a_id)}"
    print("PASS: first accepted direction stored\n")

    gen2 = make_generation(biz_a_id, "Create Diwali sale post")
    await learning.record_accepted_direction(biz_a_id, gen2)
    assert get_learned(biz_a_id) == ["Create weekend offer post", "Create Diwali sale post"], f"FAIL: {get_learned(biz_a_id)}"
    print("PASS: second distinct direction appended, order preserved\n")

    gen3 = make_generation(biz_a_id, "Create weekend offer post")  # exact dup of gen1's text
    await learning.record_accepted_direction(biz_a_id, gen3)
    assert get_learned(biz_a_id) == ["Create Diwali sale post", "Create weekend offer post"], f"FAIL: {get_learned(biz_a_id)}"
    print("PASS: duplicate text moved to most-recent instead of duplicating\n")

    print("--- Quality gate ---")
    with get_session() as db:
        biz_q = Business(phone="919999999994", name="Quality Gate Test Biz", industry="salon", onboarding_state="done")
        db.add(biz_q)
        db.flush()
        biz_q_id = biz_q.id
        db.add(BrandProfile(business_id=biz_q_id, tone="bold"))

    low_gen = make_generation(biz_q_id, "Low quality one", quality_score=50)
    await learning.record_accepted_direction(biz_q_id, low_gen)  # require_quality_threshold=True by default
    assert get_learned(biz_q_id) == [], f"FAIL: a sub-75 quality score should NOT be recorded, got {get_learned(biz_q_id)}"
    print("PASS: quality_score=50 (below 75) correctly skipped, nothing recorded\n")

    await learning.record_accepted_direction(biz_q_id, low_gen, require_quality_threshold=False)
    assert get_learned(biz_q_id) == ["Low quality one"], f"FAIL: require_quality_threshold=False should bypass the gate, got {get_learned(biz_q_id)}"
    print("PASS: require_quality_threshold=False correctly bypasses the gate (Instagram-tap behavior)\n")

    high_gen = make_generation(biz_q_id, "High quality one", quality_score=80)
    await learning.record_accepted_direction(biz_q_id, high_gen)
    assert get_learned(biz_q_id) == ["Low quality one", "High quality one"], f"FAIL: {get_learned(biz_q_id)}"
    print("PASS: quality_score=80 (above 75) correctly recorded\n")

    print("--- Distillation ---")
    with get_session() as db:
        biz_d = Business(phone="919999999993", name="Distill Test Biz", industry="retail", onboarding_state="done")
        db.add(biz_d)
        db.flush()
        biz_d_id = biz_d.id
        db.add(BrandProfile(business_id=biz_d_id, tone="friendly"))

    for i in range(1, 9):  # exactly MAX_LEARNED_PREFERENCES (8) — should NOT trigger distillation yet
        g = make_generation(biz_d_id, f"Distill test post {i}")
        await learning.record_accepted_direction(biz_d_id, g)
    learned = get_learned(biz_d_id)
    assert len(learned) == 8, f"FAIL: expected exactly 8 entries with no distillation yet, got {len(learned)}: {learned}"
    with get_session() as db:
        p = db.query(BrandProfile).filter(BrandProfile.business_id == biz_d_id).first()
        assert (p.extras or {}).get("style_summary") is None, "FAIL: style_summary should not exist before the cap is exceeded"
    print(f"PASS: exactly at cap (8 entries), no distillation triggered yet: {learned}\n")

    g9 = make_generation(biz_d_id, "Distill test post 9")
    await learning.record_accepted_direction(biz_d_id, g9)
    learned_after = get_learned(biz_d_id)
    with get_session() as db:
        p = db.query(BrandProfile).filter(BrandProfile.business_id == biz_d_id).first()
        style_summary = (p.extras or {}).get("style_summary")
    assert learned_after == ["Distill test post 9"], f"FAIL: the 9th entry should trigger distillation and reset the list to just itself, got {learned_after}"
    assert style_summary is not None, "FAIL: style_summary should be set after distillation triggers"
    assert "Distill test post 1" in style_summary, f"FAIL: fallback distillation should reference the original entries, got: {style_summary!r}"
    print(f"PASS: 9th entry triggered distillation — list reset to {learned_after}, style_summary set: {style_summary[:80]}...\n")

    print("--- Free-revision guard (logo-move pollution fix) ---")
    with get_session() as db:
        biz_f = Business(phone="919999999989", name="Free Revision Test Biz", industry="salon", onboarding_state="done")
        db.add(biz_f)
        db.flush()
        biz_f_id = biz_f.id
        db.add(BrandProfile(business_id=biz_f_id, tone="premium"))

    with get_session() as db:
        free_gen = Generation(
            business_id=biz_f_id, user_message="move logo to top-left",
            status="done", quality_score=90, credits_charged=0,  # free logo-move signature
        )
        db.add(free_gen)
        db.flush()
        free_gen_id = free_gen.id

    await learning.record_accepted_direction(biz_f_id, free_gen_id)
    assert get_learned(biz_f_id) == [], f"FAIL: a free-revision (credits_charged=0) should never be recorded, even with a high quality_score, got {get_learned(biz_f_id)}"
    print("PASS: free logo-move (credits_charged=0) correctly excluded even at quality_score=90\n")

    print("--- LearningEvent audit trail ---")
    with get_session() as db:
        events = db.query(LearningEvent).filter(LearningEvent.business_id == biz_f_id).all()
        event_types = [e.event_type for e in events]
    assert event_types == ["skipped_free_revision"], f"FAIL: expected exactly one skipped_free_revision event, got {event_types}"
    print(f"PASS: LearningEvent correctly logged: {event_types}\n")

    with get_session() as db:
        events_q = db.query(LearningEvent).filter(LearningEvent.business_id == biz_q_id).order_by(LearningEvent.id).all()
        event_types_q = [e.event_type for e in events_q]
    assert event_types_q == ["skipped_quality", "recorded", "recorded"], f"FAIL: expected [skipped_quality, recorded, recorded] for the quality-gate business, got {event_types_q}"
    print(f"PASS: quality-gate business's event history is correct: {event_types_q}\n")


asyncio.run(part_a())

print("=" * 60)
print("PART B: integration through the real orchestrator")
print("=" * 60)

from app.whatsapp import client as wa_client  # noqa: E402

sent_texts, sent_images = [], []


async def fake_send_text(to, body):
    sent_texts.append(body)


async def fake_send_image(to, image_url, caption=""):
    sent_images.append(image_url)


wa_client.send_text = fake_send_text
wa_client.send_image = fake_send_image

from app.engine import orchestrator  # noqa: E402
orchestrator.send_text = fake_send_text
orchestrator.send_image = fake_send_image

from app.engine import intent as intent_engine  # noqa: E402


async def fake_classify(user_message):
    if "brighter" in user_message.lower():
        return {"intent": "REVISE", "brief": user_message}
    return {"intent": "GENERATE", "brief": user_message}


intent_engine.classify = fake_classify
orchestrator.intent_engine.classify = fake_classify

from app.engine import concept_proposal  # noqa: E402


async def fake_decide(ctx, user_message):
    return {"decision": "SPECIFIC_ENOUGH", "brief": user_message}


concept_proposal.decide = fake_decide
orchestrator.concept_proposal.decide = fake_decide

from app.engine import revision_classifier  # noqa: E402


async def fake_rev_classify(user_message):
    return {"revision_type": "FULL_REGENERATION", "brief": user_message}


revision_classifier.classify = fake_rev_classify
orchestrator.revision_classifier.classify = fake_rev_classify

from app.engine import prompt_builder  # noqa: E402


async def fake_build(ctx, user_brief):
    return {"image_prompt": f"creative: {user_brief}", "notes_for_caption": user_brief}


prompt_builder.build = fake_build
orchestrator.prompt_builder.build = fake_build

from app.engine import image_gen  # noqa: E402


async def fake_generate_images(prompt, count=2, reference_image=None):
    return [png_bytes(), png_bytes()]


image_gen.generate_images = fake_generate_images
orchestrator.image_gen.generate_images = fake_generate_images

from app.engine import quality  # noqa: E402


async def fake_score_high(images):
    return {"best_index": 0, "best_score": 85, "issues": []}  # always passes, no regen involved


quality.score_and_pick = fake_score_high
orchestrator.quality.score_and_pick = fake_score_high

from app.engine import caption as caption_engine  # noqa: E402


async def fake_caption_generate(ctx, notes_for_caption):
    return {"caption": f"Check this out! {notes_for_caption}", "hashtags": "#offer"}


caption_engine.generate = fake_caption_generate
orchestrator.caption_engine.generate = fake_caption_generate


def fake_upload_creative(business_id, generation_id, image_bytes):
    return f"https://fake-cdn.example.com/{business_id}/{generation_id}.png"


def fake_upload_base_image(business_id, generation_id, image_bytes):
    return f"https://fake-cdn.example.com/{business_id}/{generation_id}_base.png"


orchestrator.upload_creative = fake_upload_creative
orchestrator.upload_base_image = fake_upload_base_image

from app.credits import add_credits  # noqa: E402
from app.engine.orchestrator import generate  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402


async def run():
    phone = "919999999995"
    with get_session() as db:
        biz_b = Business(phone=phone, name="Integration Test Biz", industry="salon", onboarding_state="done")
        db.add(biz_b)
        db.flush()
        biz_b_id = biz_b.id
        db.add(BrandProfile(business_id=biz_b_id, tone="premium"))
        add_credits(db, biz_b_id, 20, reason="signup_bonus")

    print("Step 1: first GENERATE ('Create post A') -- nothing to record yet")
    await generate(biz_b_id, IncomingMessage(sender=phone, type="text", text="Create post A"))
    assert get_learned(biz_b_id) == [], f"FAIL: nothing should be recorded before any prior generation exists, got {get_learned(biz_b_id)}"
    print("PASS: no prior generation, nothing recorded\n")

    print("Step 2: second GENERATE ('Create post B') -- should record post A as accepted")
    await generate(biz_b_id, IncomingMessage(sender=phone, type="text", text="Create post B"))
    assert get_learned(biz_b_id) == ["Create post A"], f"FAIL: expected post A recorded, got {get_learned(biz_b_id)}"
    print("PASS: fresh GENERATE with no prior revision correctly recorded post A\n")

    print("Step 3: REVISE ('make it brighter') on post B -- must NOT record post B")
    await generate(biz_b_id, IncomingMessage(sender=phone, type="text", text="make it brighter"))
    assert get_learned(biz_b_id) == ["Create post A"], f"FAIL: a REVISE should not record anything new, got {get_learned(biz_b_id)}"
    print("PASS: revision correctly did NOT record post B (it was rejected, not accepted)\n")

    print("Step 4: third GENERATE ('Create post C') -- should record the REVISION's request ('make it brighter')")
    await generate(biz_b_id, IncomingMessage(sender=phone, type="text", text="Create post C"))
    assert get_learned(biz_b_id) == ["Create post A", "make it brighter"], f"FAIL: got {get_learned(biz_b_id)}"
    print("PASS: the final accepted state of the revision chain was recorded, not the original\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
