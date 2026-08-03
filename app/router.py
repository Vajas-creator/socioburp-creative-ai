"""
The router is deliberately NOT an AI call — it's a plain decision tree.
Save Claude calls for where they actually add value (intent, prompts, captions).
"""
import logging

from app.db import get_session
from app.models import Business
from app.schemas import IncomingMessage
from app.whatsapp.client import send_text
from app import onboarding, credits, payments

logger = logging.getLogger("socioburp.router")


def get_or_create_business(db, phone: str) -> Business:
    biz = db.query(Business).filter(Business.phone == phone).first()
    if biz is None:
        biz = Business(phone=phone, onboarding_state="new")
        db.add(biz)
        db.flush()  # get biz.id without committing yet
        logger.info("New business created for phone=%s id=%s", phone, biz.id)
    return biz


async def handle_message(msg: IncomingMessage):
    """
    Entry point called (async, in background) for every parsed incoming message.
    """
    try:
        with get_session() as db:
            biz = get_or_create_business(db, msg.sender)
            db.commit()  # ensure biz.id exists even if something below fails
            biz_id = biz.id
            onboarding_state = biz.onboarding_state

        # --- Onboarding takes priority over everything else ---
        if onboarding_state != "done":
            await onboarding.advance(biz_id, msg)
            return

        # --- Global keywords, available any time post-onboarding ---
        text_lower = (msg.text or "").strip().lower()

        if text_lower in ("credits", "balance"):
            bal = credits.get_balance(biz_id)
            await send_text(msg.sender, f"💳 You have {bal} credits remaining.")
            return

        if text_lower == "topup":
            await payments.send_topup_options(biz_id, msg.sender)
            return

        if msg.button_id and msg.button_id.startswith("pack_"):
            # button reply from the topup options — must check button_id,
            # not text (text is the button's display title, e.g. "50 credits",
            # which never starts with "pack_")
            await payments.handle_pack_selection(biz_id, msg.sender, msg.button_id)
            return

        if msg.button_id and msg.button_id.startswith("post_ig_"):
            # "Post to Instagram" reply from a delivered creative —
            # button_id format is post_ig_<generation_id>
            import uuid as _uuid
            from app import instagram

            gen_id_str = msg.button_id[len("post_ig_"):]
            try:
                generation_id = _uuid.UUID(gen_id_str)
            except ValueError:
                logger.warning("Malformed post_ig_ button_id: %s", msg.button_id)
                await send_text(msg.sender, "Something went wrong with that button 🙏 Please try generating again.")
                return
            await instagram.handle_post_request(biz_id, msg.sender, generation_id)
            return

        if text_lower == "history":
            from app.history import send_recent_generations
            await send_recent_generations(biz_id, msg.sender)
            return

        # --- Credit check before anything that costs money ---
        if credits.get_balance(biz_id) < 1:
            await payments.send_topup_options(
                biz_id, msg.sender,
                prefix="You're out of credits! 🙏 "
            )
            return

        # --- The main event: generate or revise a creative ---
        from app.engine.orchestrator import generate
        await generate(biz_id, msg)

    except Exception:
        logger.exception("Unhandled error processing message from %s", msg.sender)
        await send_text(
            msg.sender,
            "Something went wrong on our end 🙏 No credits were charged. "
            "Please try again in a moment, or type 'help' if it keeps happening.",
        )
