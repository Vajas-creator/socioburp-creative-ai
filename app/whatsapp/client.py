"""
Thin wrapper around the WhatsApp Cloud API send endpoint.
Docs: https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages
"""
import logging

import httpx

from app.config import settings

logger = logging.getLogger("socioburp.whatsapp")

BASE_URL = f"https://graph.facebook.com/{settings.WA_API_VERSION}/{settings.WA_PHONE_NUMBER_ID}/messages"
HEADERS = {
    "Authorization": f"Bearer {settings.WA_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}


async def _post(payload: dict):
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(BASE_URL, headers=HEADERS, json=payload)
        if resp.status_code >= 400:
            logger.error("WhatsApp send failed: %s | %s", resp.status_code, resp.text)
        else:
            logger.info("WhatsApp send ok: %s", resp.json().get("messages", [{}])[0].get("id"))
        return resp


async def send_text(to: str, body: str):
    """Send a plain text message. `to` is the recipient's phone in international format, no '+'."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    return await _post(payload)


async def send_image(to: str, image_url: str, caption: str = ""):
    """Send an image by URL (must be publicly reachable — our R2 public URL works)."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": {"link": image_url, "caption": caption[:1024]},  # WA caption limit
    }
    return await _post(payload)


async def send_image_with_button(to: str, image_url: str, body: str, button_id: str, button_label: str):
    """
    Deliver a creative as an interactive message with one reply button
    (e.g. "Post to Instagram"), instead of a plain image. `body` is the
    caption text shown below the image. `button_label` must be <= 20 chars
    (WhatsApp limit).
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "header": {"type": "image", "image": {"link": image_url}},
            "body": {"text": body[:1024]},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": button_id, "title": button_label[:20]}}
                ]
            },
        },
    }
    return await _post(payload)


async def send_buttons(to: str, body: str, buttons: list[tuple[str, str]]):
    """
    Send up to 3 quick-reply buttons.
    `buttons` is a list of (id, label) tuples, e.g. [("restaurant", "Restaurant"), ("salon", "Salon")]
    Labels must be <= 20 chars (WhatsApp limit).
    """
    buttons = buttons[:3]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": bid, "title": label[:20]}}
                    for bid, label in buttons
                ]
            },
        },
    }
    return await _post(payload)


async def download_media(media_id: str) -> bytes:
    """
    Two-step download per WhatsApp API: first resolve media_id -> temporary URL,
    then fetch the bytes from that URL (also needs the auth header).
    Used for logo uploads during onboarding.
    """
    meta_url = f"https://graph.facebook.com/{settings.WA_API_VERSION}/{media_id}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        meta_resp = await client.get(meta_url, headers=HEADERS)
        meta_resp.raise_for_status()
        media_url = meta_resp.json()["url"]

        file_resp = await client.get(media_url, headers=HEADERS)
        file_resp.raise_for_status()
        return file_resp.content
