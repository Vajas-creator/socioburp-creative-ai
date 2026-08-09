"""
The router is deliberately NOT an AI call — it's a plain decision tree.
Save Claude calls for where they actually add value (intent, prompts, captions).

Concurrency: FastAPI dispatches each webhook event via BackgroundTasks
(see app/whatsapp/webhook.py) — meaning if a client sends two WhatsApp
messages close together (very common: people send "wait no" right after
their first message), two independent handle_message() calls can run
CONCURRENTLY for the same business, with no ordering guarantee. Without
serialization, two concurrent calls can read the same ConversationState
(pending_proposal, last_generation_id) and race to write it back — a
classic lost-update bug that can silently double-charge a credit, lose an
adjust_count increment, or point a revision at the wrong parent generation.

Fix: a per-business asyncio.Lock, acquired before any state-touching logic
runs. Different businesses still process fully in parallel; messages for
the SAME business are now strictly sequential. This is an in-process lock
— correct for the current single Render instance. If this ever scales to
multiple instances/processes, this needs to become a distributed lock
(e.g. a Postgres advisory lock via Neon) instead — noted here so it isn't
silently wrong later.
"""
import asyncio
import logging
import uuid

from app.db import get_session
from app.models import Business
from app.schemas import IncomingMessage
from app.whatsapp.client import send_text
from app import onboarding, credits, payments, persona, i18n

logger = logging.getLogger("socioburp.router")

# Bare greetings from an already-onboarded client -- "hi" with no actual
# request in it. Deliberately a plain keyword set, not an intent-classifier
# call: this file is a plain decision tree by design (see module docstring),
# and a returning user just saying hello is exactly the cheap, unambiguous
# case that doesn't need a Claude call to recognize.
BARE_GREETINGS = {"hi", "hey", "hello", "hii", "hiii", "heya", "hola", "yo"}

_business_locks: dict[uuid.UUID, asyncio.Lock] = {}
_locks_registry_lock = asyncio.Lock()  # protects creation of new per-business locks only


async def _get_business_lock(business_id: uuid.UUID) -> asyncio.Lock:
    async with _locks_registry_lock:
        if business_id not in _business_locks:
            _business_locks[business_id] = asyncio.Lock()
        return _business_locks[business_id]


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

        # Everything from here on touches per-business state (onboarding
        # progress, pending_proposal, last_generation_id, credits) — hold
        # this business's lock for the rest of the message so a second,
        # near-simultaneous message from the same client can't race it.
        lock = await _get_business_lock(biz_id)
        async with lock:
            await _process_message(biz_id, msg)

    except Exception:
        logger.exception("Unhandled error processing message from %s", msg.sender)
        await send_text(
            msg.sender,
            "Something went wrong on our end 🙏 No credits were charged. "
            "Please try again in a moment, or type 'help' if it keeps happening.",
        )


async def _process_message(biz_id: uuid.UUID, msg: IncomingMessage):
    """The actual routing logic — runs under this business's lock. Not called directly; see handle_message()."""
    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id).first()
        onboarding_state = biz.onboarding_state

    # --- Onboarding takes priority over everything else ---
    if onboarding_state != "done":
        await onboarding.advance(biz_id, msg)
        return

    # --- Global keywords, available any time post-onboarding ---
    text_lower = (msg.text or "").strip().lower()

    # A returning user (already onboarded, hence past the state check
    # above) saying just "hi" gets a short, direct prompt -- never routed
    # back into onboarding (that only ever happens for a business with no
    # profile yet, per the state check above) and never left to fall
    # through to the generic OTHER-intent fallback in orchestrator.generate().
    if text_lower in BARE_GREETINGS:
        await send_text(msg.sender, "What do you want to create today? 🎨")
        return

    if persona.is_identity_question(msg.text or ""):
        with get_session() as db:
            biz = db.query(Business).filter(Business.id == biz_id).first()
            language = biz.preferred_language or "en"
        reply = await i18n.t("identity_disclosure", language, persona.DISCLOSURE_TEXT)
        await send_text(msg.sender, reply)
        return

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
