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
        # Pull everything into plain values while the session is still open —
        # gen becomes unusable (DetachedInstanceError) once we leave this
        # block, since get_session() expires attributes on commit.
        recent_creatives = [(gen.image_url, gen.caption) for gen in recent]

    if not recent_creatives:
        await send_text(phone, "No creatives yet! Try: *Create a weekend offer post*")
        return

    for image_url, caption in recent_creatives:
        if image_url:
            await send_image(phone, image_url, caption=caption or "")
