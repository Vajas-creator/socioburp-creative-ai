"""
Local smoke test for the Week 2 engine — mocks Claude, image generation, and
WhatsApp sends so the full pipeline logic (intent -> prompt -> image ->
composite -> caption -> quality -> save -> charge -> deliver) can be verified
without spending real API credits or touching Postgres (uses SQLite).
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_smoke_week2.db"
os.environ["WA_VERIFY_TOKEN"] = "fake"
os.environ["WA_ACCESS_TOKEN"] = "fake"
os.environ["WA_PHONE_NUMBER_ID"] = "fake"
os.environ["ANTHROPIC_API_KEY"] = "fake"
os.environ["R2_ACCOUNT_ID"] = "fake"
os.environ["R2_ACCESS_KEY"] = "fake"
os.environ["R2_SECRET_KEY"] = "fake"
os.environ["R2_BUCKET"] = "fake"
os.environ["R2_PUBLIC_BASE_URL"] = "https://fake.example.com"
os.environ.setdefault("IMAGE_API_KEY", "fake")

from app import db as db_module
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

db_module.engine = create_engine("sqlite:///./test_smoke_week2.db")
db_module.SessionLocal = sessionmaker(bind=db_module.engine)

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


import app.models  # noqa: E402
db_module.Base.metadata.create_all(bind=db_module.engine)

from PIL import Image  # noqa: E402


def fake_png_bytes(color=(200, 50, 50)):
    img = Image.new("RGB", (1024, 1024), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --- Monkeypatch WhatsApp sends ---
from app.whatsapp import client as wa_client  # noqa: E402


async def fake_send_text(to, body):
    print(f"[SEND TEXT to {to}]:\n{body}\n")


async def fake_send_image(to, image_url, caption=""):
    print(f"[SEND IMAGE to {to}]: url={image_url}\ncaption={caption}\n")


wa_client.send_text = fake_send_text
wa_client.send_image = fake_send_image

from app.engine import orchestrator  # noqa: E402
orchestrator.send_text = fake_send_text
orchestrator.send_image = fake_send_image

from app import onboarding  # noqa: E402
onboarding.send_text = fake_send_text


async def fake_send_buttons(to, body, buttons):
    print(f"[SEND BUTTONS to {to}]: {body} | {buttons}")


onboarding.send_buttons = fake_send_buttons

from app import payments  # noqa: E402
payments.send_text = fake_send_text

# --- Monkeypatch intent classification (no real Claude call) ---
from app.engine import intent as intent_engine  # noqa: E402


async def fake_classify(user_message):
    text = user_message.lower()
    if "premium" in text or "brighter" in text or "bolder" in text:
        return {"intent": "REVISE", "brief": user_message}
    if "credit" in text or "how" in text:
        return {"intent": "QUESTION", "brief": user_message}
    return {"intent": "GENERATE", "brief": user_message}


intent_engine.classify = fake_classify
orchestrator.intent_engine.classify = fake_classify

# --- Monkeypatch prompt builder (no real Claude call) ---
from app.engine import prompt_builder  # noqa: E402


async def fake_build(ctx, user_brief):
    return {
        "image_prompt": f"A vibrant marketing creative for {ctx.name}: {user_brief}",
        "headline_text": user_brief[:30],
        "notes_for_caption": user_brief,
    }


prompt_builder.build = fake_build
orchestrator.prompt_builder.build = fake_build

# --- Monkeypatch image generation (no real API call, no real cost) ---
from app.engine import image_gen  # noqa: E402


async def fake_generate_images(prompt, count=2):
    print(f"[IMAGE GEN] prompt={prompt[:80]}... count={count}")
    return [fake_png_bytes((200, 50, 50)), fake_png_bytes((50, 150, 200))]


image_gen.generate_images = fake_generate_images
orchestrator.image_gen.generate_images = fake_generate_images

# --- Monkeypatch quality checker (no real Claude vision call) ---
from app.engine import quality  # noqa: E402


async def fake_score_and_pick(images):
    print(f"[QUALITY CHECK] scoring {len(images)} candidates")
    return {"best_index": 0, "best_score": 82, "issues": []}


quality.score_and_pick = fake_score_and_pick
orchestrator.quality.score_and_pick = fake_score_and_pick

# --- Monkeypatch caption generation (no real Claude call) ---
from app.engine import caption as caption_engine  # noqa: E402


async def fake_caption_generate(ctx, notes_for_caption):
    return {
        "caption": f"✨ Check out our latest offer! {notes_for_caption}",
        "hashtags": "#smallbusiness #india #offer #socioburp",
    }


caption_engine.generate = fake_caption_generate
orchestrator.caption_engine.generate = fake_caption_generate

# --- Monkeypatch R2 storage upload (no real bucket needed) ---
from app import storage  # noqa: E402


def fake_upload_creative(business_id, generation_id, image_bytes):
    fake_url = f"https://fake-cdn.example.com/creatives/{business_id}/{generation_id}.png"
    print(f"[R2 UPLOAD] {len(image_bytes)} bytes -> {fake_url}")
    return fake_url


storage.upload_creative = fake_upload_creative
orchestrator.upload_creative = fake_upload_creative

from app.router import handle_message  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402


async def run():
    phone = "919999999999"
    steps = [
        # Full onboarding
        IncomingMessage(sender=phone, type="text", text="hi"),
        IncomingMessage(sender=phone, type="text", text="Copper & Crumb Bakery"),
        IncomingMessage(sender=phone, type="button", button_id="restaurant", text="Restaurant"),
        IncomingMessage(sender=phone, type="text", text="skip"),
        IncomingMessage(sender=phone, type="text", text="skip"),
        IncomingMessage(sender=phone, type="button", button_id="premium", text="Premium"),
        # Real generation request
        IncomingMessage(sender=phone, type="text", text="Create a weekend offer post, 20% off all cakes"),
        # Revision request
        IncomingMessage(sender=phone, type="text", text="make it more premium"),
        # Off-topic question
        IncomingMessage(sender=phone, type="text", text="how does this work?"),
    ]

    for i, msg in enumerate(steps, 1):
        print(f"\n=== Step {i}: user sends '{msg.text or msg.button_id}' ===")
        await handle_message(msg)

    # Final DB check
    from app.db import get_session
    from app.models import Business, Generation
    from app.credits import get_balance

    with get_session() as db:
        biz = db.query(Business).filter(Business.phone == phone).first()
        gens = db.query(Generation).filter(Generation.business_id == biz.id).order_by(Generation.created_at).all()

        print("\n=== FINAL STATE ===")
        print(f"Business: {biz.name}, credits: {get_balance(biz.id)}")
        print(f"Total generations: {len(gens)}")
        for g in gens:
            print(f"  - id={str(g.id)[:8]} status={g.status} score={g.quality_score} "
                  f"parent={str(g.parent_id)[:8] if g.parent_id else None} charged={g.credits_charged}")


asyncio.run(run())
