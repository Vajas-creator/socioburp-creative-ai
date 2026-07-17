"""
Razorpay payment links for credit top-ups.

Flow:
  1. User texts "topup" -> we send 3 quick-reply buttons (credit packs)
  2. User taps a button -> we create a Razorpay Payment Link with the
     business_id + credit amount embedded in `notes`, send the link on WhatsApp
  3. User pays on Razorpay's hosted page -> Razorpay calls our webhook
     (payment_link.paid event)
  4. Webhook verifies the signature, credits the ledger, confirms on WhatsApp

Idempotency: the Razorpay payment_link id is stored as credit_ledger.ref_id.
Before crediting, we check no ledger row already has that ref_id + reason
'topup' — protects against Razorpay's webhook retry behavior double-crediting
if it fires more than once for the same payment (which it does, by design).
"""
import hashlib
import hmac
import json
import logging
import uuid

import httpx
from fastapi import APIRouter, Request, HTTPException

from app.config import settings
from app.db import get_session
from app.models import Business, CreditLedger
from app.whatsapp.client import send_text, send_buttons
from app.credits import add_credits, get_balance

logger = logging.getLogger("socioburp.payments")
router = APIRouter()

RAZORPAY_BASE = "https://api.razorpay.com/v1"

# Pricing: unit cost per generation is roughly ₹12-17 (2 images + Claude calls
# + quality check). Priced at ~2x cost with a bulk discount on larger packs.
CREDIT_PACKS = {
    "pack_50":  {"credits": 50,  "amount_paise": 79900,  "label": "50 credits — ₹799"},
    "pack_200": {"credits": 200, "amount_paise": 299900, "label": "200 credits — ₹2,999"},
    "pack_500": {"credits": 500, "amount_paise": 699900, "label": "500 credits — ₹6,999"},
}


def _auth():
    return (settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)


async def send_topup_options(business_id: uuid.UUID, phone: str, prefix: str = ""):
    buttons = [(pid, pack["label"].split(" — ")[0]) for pid, pack in CREDIT_PACKS.items()]
    body = f"{prefix}Choose a credit pack:\n\n" + "\n".join(
        f"• {p['label']}" for p in CREDIT_PACKS.values()
    )
    await send_buttons(phone, body, buttons)


async def handle_pack_selection(business_id: uuid.UUID, phone: str, pack_id: str):
    pack = CREDIT_PACKS.get(pack_id)
    if not pack:
        await send_text(phone, "Sorry, that option isn't available. Type *topup* to see packs again.")
        return

    try:
        payload = {
            "amount": pack["amount_paise"],
            "currency": "INR",
            "description": f"SocioBurp — {pack['credits']} credits",
            "notes": {"business_id": str(business_id), "credits": str(pack["credits"])},
            "notify": {"sms": False, "email": False},  # we send our own WhatsApp message
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{RAZORPAY_BASE}/payment_links", auth=_auth(), json=payload)
            resp.raise_for_status()
            link = resp.json()

        await send_text(
            phone,
            f"Here's your payment link for {pack['credits']} credits:\n\n{link['short_url']}\n\n"
            f"Credits will be added automatically once payment is confirmed ✅",
        )

    except Exception:
        logger.exception("Failed to create Razorpay payment link for business=%s pack=%s", business_id, pack_id)
        await send_text(
            phone,
            "Sorry, couldn't generate a payment link right now 🙏 Please try again in a moment, "
            "or contact us directly at Ajinu.A@socioburp.co.in.",
        )


def _verify_signature(body: bytes, signature: str) -> bool:
    if not signature:
        return False
    expected = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/razorpay/webhook")
async def razorpay_webhook(request: Request):
    """
    Configure this URL in the Razorpay Dashboard -> Settings -> Webhooks:
      https://<your-render-url>/razorpay/webhook
    Subscribe to the 'payment_link.paid' event. Use the same secret you
    generate there as RAZORPAY_WEBHOOK_SECRET in env vars.
    """
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not _verify_signature(body, signature):
        logger.warning("Razorpay webhook signature mismatch — rejecting request.")
        raise HTTPException(status_code=400, detail="Invalid signature")

    event = json.loads(body)

    if event.get("event") != "payment_link.paid":
        return {"status": "ignored"}

    payload = event["payload"]["payment_link"]["entity"]
    notes = payload.get("notes", {})
    business_id_str = notes.get("business_id")
    credits_str = notes.get("credits")
    payment_link_id = payload["id"]

    if not business_id_str or not credits_str:
        logger.warning("Razorpay webhook missing expected notes: %s", notes)
        return {"status": "ignored"}

    business_id = uuid.UUID(business_id_str)
    credits_to_add = int(credits_str)

    with get_session() as db:
        already_processed = (
            db.query(CreditLedger)
            .filter(CreditLedger.ref_id == payment_link_id, CreditLedger.reason == "topup")
            .first()
        )
        if already_processed:
            logger.info("Razorpay webhook for %s already processed — skipping duplicate credit.", payment_link_id)
            return {"status": "already_processed"}

        add_credits(db, business_id, credits_to_add, reason="topup", ref_id=payment_link_id)

    with get_session() as db:
        business = db.query(Business).filter(Business.id == business_id).first()
        phone = business.phone if business else None

    if phone:
        balance = get_balance(business_id)
        await send_text(
            phone,
            f"✅ Payment received! {credits_to_add} credits added.\n\n💳 New balance: {balance} credits",
        )

    logger.info("Credited %s credits to business=%s via payment_link=%s", credits_to_add, business_id, payment_link_id)
    return {"status": "processed"}
