"""
'history' keyword handler — sends the last 3 generated creatives.
Simple in Week 1 since the engine doesn't exist yet; will have real content
to show starting Week 2.
"""
import uuid

from app.db import get_session
from app.models import Generation
from app.whatsapp.client import send_text, send_image


async def send_recent_generations(business_id: uuid.UUID, phone: str):
    with get_session() as db:
        recent = (
            db.query(Generation)
            .filter(Generation.business_id == business_id, Generation.status == "done")
            .order_by(Generation.created_at.desc())
            .limit(3)
            .all()
        )

    if not recent:
        await send_text(phone, "No creatives yet! Try: *Create a weekend offer post*")
        return

    for gen in recent:
        if gen.image_url:
            await send_image(phone, gen.image_url, caption=gen.caption or "")
