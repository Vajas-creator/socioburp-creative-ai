"""
Local smoke test — simulates a full onboarding conversation using SQLite
instead of Postgres, and monkeypatches WhatsApp sends to print instead of
hitting the real API. Not part of the deployed app; just for verifying
the Week 1 flow works before pushing to Render.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_smoke.db"

from app import db as db_module
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

db_module.engine = create_engine("sqlite:///./test_smoke.db")
db_module.SessionLocal = sessionmaker(bind=db_module.engine)

# JSONB is Postgres-only; swap to generic JSON so this smoke test can run on
# SQLite. Production (Render/Neon) uses real Postgres, so JSONB is used there
# via the actual models.py — this patch only affects this local test run.
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


import app.models  # noqa: E402,F401  register models on Base
db_module.Base.metadata.create_all(bind=db_module.engine)

from app.whatsapp import client as wa_client  # noqa: E402


async def fake_send_text(to, body):
    print(f"[SEND TEXT to {to}]:\n{body}\n")


async def fake_send_buttons(to, body, buttons):
    print(f"[SEND BUTTONS to {to}]: {body} | options={buttons}\n")


wa_client.send_text = fake_send_text
wa_client.send_buttons = fake_send_buttons

from app import onboarding  # noqa: E402
onboarding.send_text = fake_send_text
onboarding.send_buttons = fake_send_buttons

from app import payments  # noqa: E402
payments.send_text = fake_send_text

from app.router import handle_message  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402


async def run():
    steps = [
        IncomingMessage(sender="919999999999", type="text", text="hi"),
        IncomingMessage(sender="919999999999", type="text", text="Copper & Crumb"),
        IncomingMessage(sender="919999999999", type="button", button_id="restaurant", text="Restaurant"),
        IncomingMessage(sender="919999999999", type="text", text="skip"),
        IncomingMessage(sender="919999999999", type="text", text="skip"),
        IncomingMessage(sender="919999999999", type="button", button_id="premium", text="Premium"),
        IncomingMessage(sender="919999999999", type="text", text="credits"),
        IncomingMessage(sender="919999999999", type="text", text="Create a weekend offer post"),
    ]
    for i, msg in enumerate(steps, 1):
        print(f"--- Step {i}: user sends '{msg.text or msg.button_id}' ---")
        await handle_message(msg)

    # Final DB check
    from app.db import get_session
    from app.models import Business, BrandProfile
    from app.credits import get_balance

    with get_session() as db:
        biz = db.query(Business).filter(Business.phone == "919999999999").first()
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == biz.id).first()
        print("=== FINAL STATE ===")
        print(f"Business: name={biz.name} industry={biz.industry} state={biz.onboarding_state}")
        print(f"Brand profile: tone={profile.tone} logo={profile.logo_url} color={profile.primary_color}")
        print(f"Credit balance: {get_balance(biz.id)}")


asyncio.run(run())
