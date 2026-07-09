"""
Webhook endpoints Meta calls.

GET  /webhook  — one-time verification handshake when you configure the webhook in Meta dashboard
POST /webhook  — every incoming message/event

CRITICAL: POST must return within ~2 seconds or Meta will retry and you'll
process the same message multiple times. We ack immediately and do the real
work in a BackgroundTask.
"""
import logging

from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.router import handle_message
from app.schemas import IncomingMessage

logger = logging.getLogger("socioburp.webhook")
router = APIRouter()


@router.get("/webhook")
async def verify(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.WA_VERIFY_TOKEN:
        logger.info("Webhook verified successfully.")
        return PlainTextResponse(challenge)

    logger.warning("Webhook verification failed — token mismatch.")
    raise HTTPException(status_code=403, detail="Verification failed")


def parse_message(payload: dict) -> IncomingMessage | None:
    """
    Extract the useful bits from Meta's deeply nested webhook payload.
    Returns None for events we don't care about (delivery receipts, etc.)
    """
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]
        value = change["value"]

        if "messages" not in value:
            # status update (sent/delivered/read) or something else — ignore
            return None

        msg = value["messages"][0]
        sender = msg["from"]  # phone number, no '+'
        msg_type = msg["type"]

        if msg_type == "text":
            return IncomingMessage(sender=sender, type="text", text=msg["text"]["body"])

        if msg_type == "image":
            return IncomingMessage(sender=sender, type="image", media_id=msg["image"]["id"])

        if msg_type == "interactive":
            interactive = msg["interactive"]
            if interactive["type"] == "button_reply":
                return IncomingMessage(
                    sender=sender,
                    type="button",
                    button_id=interactive["button_reply"]["id"],
                    text=interactive["button_reply"]["title"],
                )

        logger.info("Unhandled message type: %s", msg_type)
        return None

    except (KeyError, IndexError) as e:
        logger.warning("Could not parse webhook payload: %s | payload=%s", e, payload)
        return None


@router.post("/webhook")
async def receive(request: Request, background: BackgroundTasks):
    payload = await request.json()
    msg = parse_message(payload)

    if msg:
        # Fire and forget — respond to Meta immediately, process async
        background.add_task(handle_message, msg)

    return {"status": "ok"}
