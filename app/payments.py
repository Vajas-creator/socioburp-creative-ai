"""
Placeholder for Week 3. Full implementation will:
  - send 3 credit-pack buttons
  - create a Razorpay Payment Link on button tap
  - expose a /razorpay/webhook route (see router registration in main.py)
  - verify webhook signature, insert credit_ledger row, confirm on WhatsApp

Stubbed now so app.router can import without errors during Week 1 testing.
"""
import logging
import uuid

from fastapi import APIRouter

from app.whatsapp.client import send_text

logger = logging.getLogger("socioburp.payments")
router = APIRouter()


async def send_topup_options(business_id: uuid.UUID, phone: str, prefix: str = ""):
    await send_text(
        phone,
        f"{prefix}💳 Top-up options are coming very soon! "
        "For now, contact us directly at Ajinu.A@socioburp.co.in to add credits.",
    )


async def handle_pack_selection(business_id: uuid.UUID, phone: str, pack_id: str):
    await send_text(phone, "Payments are being finalized — thanks for your patience! 🙏")
