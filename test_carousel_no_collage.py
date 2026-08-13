"""
Regression test for the "carousel produces one collage image instead of N
separate photos" bug -- reported TWICE: once in the original Aug 2026
live-test report (traced then to requests falling through to the old
single-image pipeline, since fixed), and again after the full carousel
rewrite, from a live screenshot showing a single professionally-composed
6-panel image (title panel + 5 content panels, each with its own heading
and body text) delivered as "the carousel."

Root cause the second time: each slide's brief, sent to prompt_builder.py,
literally said "This is slide X of N in an Instagram carousel post... a
consistent style with the REST OF THE CAROUSEL" -- Claude (composing the
actual image_prompt) reasonably read "carousel" as a design genre and
produced a prompt for a multi-panel carousel PREVIEW/mockup as ONE image,
a real, common stock-template style -- not as "this app's delivery
mechanism is N separately-generated photos." All N slides plausibly
generated near-identical collages; the one used as the WhatsApp preview
thumbnail (the first slide) is what showed up as "the carousel."

Fixed by rewording the per-slide brief to never say "carousel" or "slide
X of N," framing each image purely as one standalone photo that happens
to be part of a separately-delivered themed set, AND by adding an
explicit "ONE SCENE ONLY, never a collage/grid/multi-panel layout" rule
to prompt_builder.py's SYSTEM_PROMPT itself, as defense in depth for any
other caller that might reference "series"/"set"/"carousel" in a brief.

Covers:
  - The brief passed to prompt_builder.build() for a multi-slide carousel
    never contains the word "carousel", and never says "slide N of M".
  - It explicitly forbids a collage/grid/multi-panel/mockup response.
  - It still conveys "keep a consistent style across the set" without
    implying the other images should be depicted.
  - A single-slide ("carousel" of 1) request is unaffected -- the brief
    is just the plain content description, unchanged from a normal
    single-image request.
  - prompt_builder.py's SYSTEM_PROMPT itself contains an explicit,
    general anti-collage rule (defense in depth, not just carousel-specific).
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_carousel_no_collage.db"
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


async def fake_send_text(to, body):
    pass


async def fake_send_image(to, image_url, caption=""):
    pass


async def fake_send_image_with_button(to, image_url, body, button_id, button_label):
    pass


wa_client.send_text = fake_send_text
wa_client.send_image = fake_send_image
wa_client.send_image_with_button = fake_send_image_with_button

from app.engine import orchestrator as orch  # noqa: E402
orch.send_text = fake_send_text
orch.send_image = fake_send_image
orch.send_image_with_button = fake_send_image_with_button

from app.engine import prompt_builder  # noqa: E402

prompt_builder_calls = []


async def fake_build(ctx, user_brief):
    prompt_builder_calls.append(user_brief)
    return {"image_prompt": f"prompt: {user_brief}", "headline_text": "Sale", "notes_for_caption": user_brief}


prompt_builder.build = fake_build
orch.prompt_builder.build = fake_build

from app.engine import image_gen  # noqa: E402


async def fake_generate_images(prompt, count=2, reference_image=None):
    return [png_bytes()] * count


image_gen.generate_images = fake_generate_images
orch.image_gen.generate_images = fake_generate_images

from app.engine import quality  # noqa: E402


async def fake_score_and_pick(images):
    return {"best_index": 0, "best_score": 90, "issues": []}


quality.score_and_pick = fake_score_and_pick
orch.quality.score_and_pick = fake_score_and_pick

from app.engine import caption as caption_engine  # noqa: E402


async def fake_caption_generate(ctx, notes_for_caption):
    return {"caption": "Nice!", "hashtags": "#offer"}


caption_engine.generate = fake_caption_generate
orch.caption_engine.generate = fake_caption_generate


def fake_upload_carousel_slide(business_id, generation_id, slide_num, image_bytes):
    return f"https://fake.example.com/creatives/{generation_id}_slide{slide_num}.png"


orch.upload_carousel_slide = fake_upload_carousel_slide

from app.db import get_session  # noqa: E402
from app.models import Business, BrandProfile  # noqa: E402
from app.engine.context import BusinessContext  # noqa: E402
from app.credits import add_credits  # noqa: E402


def _make_business(phone):
    with get_session() as db:
        biz = Business(phone=phone, name="Resin Decor Co", industry="home decor", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id
        db.add(BrandProfile(business_id=biz_id, tone="elegant"))
        add_credits(db, biz_id, 20, reason="signup_bonus")
        return biz_id


def _ctx():
    return BusinessContext(name="Resin Decor Co", industry="home decor", tone="elegant")


async def run():
    print("=" * 60)
    print("TEST 1: a multi-slide carousel's per-slide briefs never say 'carousel' or 'slide N of M'")
    print("=" * 60)
    phone = "919999999901"
    biz_id = _make_business(phone)
    prompt_builder_calls.clear()

    slide_briefs = [
        "Resin Decor title slide with brand name",
        "Every piece is handmade close-up shot",
        "Material Beauty texture detail",
        "Inspired by Nature styling shot",
        "Made by Hand, Made to Last lifestyle shot",
        "Crafted with heart closing shot",
    ]

    await orch.generate_carousel(
        biz_id, phone, _ctx(), slide_briefs, user_message="6-image carousel for our resin decor line",
    )

    assert len(prompt_builder_calls) == 6, f"FAIL: expected 6 prompt_builder calls, got {len(prompt_builder_calls)}"
    for i, brief in enumerate(prompt_builder_calls, start=1):
        assert "carousel" not in brief.lower(), (
            f"FAIL: slide {i}'s brief still mentions 'carousel' -- this is exactly what caused the "
            f"collage bug (Claude reads it as 'design a carousel preview/mockup as one image'), got {brief!r}"
        )
        assert f"slide {i} of" not in brief.lower(), (
            f"FAIL: slide {i}'s brief still uses 'slide N of M' framing, got {brief!r}"
        )
        assert "not a collage" in brief.lower() or "standalone image" in brief.lower(), (
            f"FAIL: expected the brief to explicitly rule out a collage/grid, got {brief!r}"
        )
        assert slide_briefs[i - 1] in brief, f"FAIL: expected slide {i}'s own content in its brief, got {brief!r}"
    print("PASS: no slide's brief mentions 'carousel' or 'slide N of M'; every brief explicitly rules out a collage\n")

    print("=" * 60)
    print("TEST 2: a single-slide 'carousel' (N=1) brief is just the plain content, unchanged")
    print("=" * 60)
    phone2 = "919999999902"
    biz_id2 = _make_business(phone2)
    prompt_builder_calls.clear()

    await orch.generate_carousel(biz_id2, phone2, _ctx(), ["A single hero shot of our new candle"], user_message="one image please")

    assert prompt_builder_calls == ["A single hero shot of our new candle"], (
        f"FAIL: expected the single-slide brief unchanged (no carousel framing needed for N=1), got {prompt_builder_calls}"
    )
    print(f"PASS: N=1 brief is plain and unwrapped: {prompt_builder_calls[0]!r}\n")

    print("=" * 60)
    print("TEST 3: prompt_builder.py's own SYSTEM_PROMPT has an explicit, general anti-collage rule")
    print("=" * 60)
    system_lower = prompt_builder.SYSTEM_PROMPT.lower()
    assert "collage" in system_lower, "FAIL: expected an explicit anti-collage rule in prompt_builder's SYSTEM_PROMPT"
    assert "grid" in system_lower, "FAIL: expected 'grid' called out explicitly"
    assert "multi-panel" in system_lower or "multi panel" in system_lower, "FAIL: expected 'multi-panel' called out explicitly"
    print("PASS: prompt_builder.py's SYSTEM_PROMPT explicitly forbids collage/grid/multi-panel output as defense in depth\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
