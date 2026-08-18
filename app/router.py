"""
Top-level message dispatch. Structural checks (onboarding state, an
in-progress negotiation, button taps, message type) are still plain
control flow, no AI involved. But what KIND of message this text is
(greeting / identity question / global command / carousel request /
cancel / a normal request) is decided by one Claude call per message --
see app/engine/router_intent.py -- instead of the exact-keyword cascade
this used to be. That cascade's blind spot (a misspelled "carousel"
silently fell through to the wrong pipeline entirely) turned out to be
systemic, not a one-off; see that module's docstring for the full
reasoning and the cost/latency tradeoff being made here.

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
from app.models import Business, ConversationState
from app.schemas import IncomingMessage
from app.whatsapp.client import send_text
from app import onboarding, credits, payments, persona, i18n, analytics, allowlist, alerting, agentic_beta
from app.engine import router_intent

logger = logging.getLogger("socioburp.router")

_business_locks: dict[uuid.UUID, asyncio.Lock] = {}
_locks_registry_lock = asyncio.Lock()  # protects creation of new per-business locks only


async def _get_business_lock(business_id: uuid.UUID) -> asyncio.Lock:
    async with _locks_registry_lock:
        if business_id not in _business_locks:
            _business_locks[business_id] = asyncio.Lock()
        return _business_locks[business_id]


def get_or_create_business(db, phone: str) -> tuple[Business, bool]:
    """Returns (business, is_new) -- is_new is what triggers the 'signup' analytics event, see handle_message()."""
    biz = db.query(Business).filter(Business.phone == phone).first()
    if biz is None:
        biz = Business(phone=phone, onboarding_state="new")
        db.add(biz)
        db.flush()  # get biz.id without committing yet
        logger.info("New business created for phone=%s id=%s", phone, biz.id)
        return biz, True
    return biz, False


async def handle_message(msg: IncomingMessage):
    """
    Entry point called (async, in background) for every parsed incoming message.
    """
    try:
        with get_session() as db:
            biz, is_new = get_or_create_business(db, msg.sender)
            db.commit()  # ensure biz.id exists even if something below fails
            biz_id = biz.id

        if is_new:
            # Logged AFTER the commit above, not before -- analytics.log_event()
            # opens its own session/connection, and would hit a foreign-key
            # violation trying to reference a business row that isn't durably
            # committed yet from another connection's point of view.
            analytics.log_event(biz_id, "signup")

        # Everything from here on touches per-business state (onboarding
        # progress, pending_proposal, last_generation_id, credits) — hold
        # this business's lock for the rest of the message so a second,
        # near-simultaneous message from the same client can't race it.
        lock = await _get_business_lock(biz_id)
        async with lock:
            await _process_message(biz_id, msg)

    except Exception as exc:
        logger.exception("Unhandled error processing message from %s", msg.sender)
        await alerting.send_alert(
            "unhandled_message_error",
            f"Unhandled error processing a message from {msg.sender}: {exc!r}",
        )
        await send_text(
            msg.sender,
            "Something went wrong on our end 🙏 No credits were charged. "
            "Please try again in a moment, or type 'help' if it keeps happening.",
        )


async def _process_message(biz_id: uuid.UUID, msg: IncomingMessage):
    """The actual routing logic — runs under this business's lock. Not called directly; see handle_message()."""
    # --- Agentic-beta bot: an allowlisted business's messages skip the
    # entire classic pipeline below (onboarding state machine, intent
    # classification, carousel/image-intent negotiation) and go straight
    # to app/engine/agent.py's continuous, tool-calling conversation
    # instead -- see app/agentic_beta.py. Checked before even reading
    # onboarding_state, since the agent handles introductions itself.
    if agentic_beta.is_enabled(msg.sender):
        from app.engine import agent
        await agent.handle_message(biz_id, msg)
        return

    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id).first()
        onboarding_state = biz.onboarding_state
        # Owner's own name (asked once during onboarding, purely for
        # personalization) is preferred over the business name for
        # addressing them directly -- "Hey Priya!" reads more like a
        # person than "Hey Copper & Crumb!".
        display_name = biz.owner_name or biz.name
        convo = db.query(ConversationState).filter(ConversationState.business_id == biz_id).first()
        pending_carousel = convo.pending_carousel if convo else None
        pending_image_intent = convo.pending_image_intent if convo else None

    # --- Onboarding takes priority over everything else ---
    if onboarding_state != "done":
        result = await onboarding.advance(biz_id, msg)
        if result is not None:
            # Onboarding just completed -- run the auto-generation it
            # promised ("Give me a moment") here, now that advance()'s own
            # DB session has fully closed. Must NOT run this while that
            # session is still open -- see onboarding.advance()'s
            # docstring for why. last_generation_id=None is correct: this
            # is genuinely this business's first-ever generation.
            ctx, brief = result
            from app.engine.orchestrator import _run_generation
            await _run_generation(
                biz_id, msg.sender, ctx, brief, brief,
                last_generation_id=None, is_revision=False,
                trigger_source="onboarding_complete",
            )
        return

    # Button replies are WhatsApp-generated IDs, never typed by the user --
    # no typo risk, no reason to spend a classification call on them.
    # Checked before classification so a button tap never triggers one.
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

    # One classification call drives everything below -- see
    # app/engine/router_intent.py. Skips the Claude call itself (returns
    # OTHER) for empty text, e.g. an image upload with no caption.
    classification = await router_intent.classify(msg.text)
    intent = classification["intent"]

    # --- An in-progress carousel negotiation (slide count / per-slide
    # content) or "what should I do with this photo" negotiation takes
    # priority over everything below, same principle as onboarding above
    # -- every reply belongs to that negotiation until it finishes or the
    # client cancels. See app/engine/carousel.py / app/engine/image_intent.py.
    # EXCEPT: an explicit cancel, an unambiguous global command, or
    # (mid-image-upload only) explicitly asking for a carousel -- all read
    # as "switch context", not an answer to the pending question, so the
    # negotiation is dropped and the message falls through to be handled
    # normally instead. Saying "carousel" again mid-CAROUSEL-negotiation is
    # deliberately NOT treated as a topic switch -- that's redundant, not a
    # switch, and could plausibly be genuine slide content.
    if pending_carousel or pending_image_intent:
        if intent == "CANCEL":
            with get_session() as db:
                convo = db.query(ConversationState).filter(ConversationState.business_id == biz_id).first()
                if convo:
                    convo.pending_carousel = None
                    convo.pending_image_intent = None
            cancel_reply = (
                "No worries, carousel cancelled 👍 Let me know if you'd like to try again."
                if pending_carousel else
                "No worries — let me know if you'd like to do something with it later 👍"
            )
            await send_text(msg.sender, cancel_reply)
            return

        if intent in ("GLOBAL_COMMAND", "LOGO_UPLOAD"):
            with get_session() as db:
                convo = db.query(ConversationState).filter(ConversationState.business_id == biz_id).first()
                if convo:
                    convo.pending_carousel = None
                    convo.pending_image_intent = None
            pending_carousel = None
            pending_image_intent = None
            # falls through to the GLOBAL_COMMAND/LOGO_UPLOAD handling below

        elif pending_image_intent and intent == "CAROUSEL_REQUEST":
            with get_session() as db:
                convo = db.query(ConversationState).filter(ConversationState.business_id == biz_id).first()
                if convo:
                    convo.pending_image_intent = None
            pending_image_intent = None
            # falls through to the CAROUSEL_REQUEST handling below

        else:
            if pending_carousel:
                from app.engine import carousel
                await carousel.advance(biz_id, msg, pending_carousel)
                return
            from app.engine import image_intent
            await image_intent.advance(biz_id, msg, pending_image_intent)
            return

    # Instrumentation only, no behavior change -- logs 'user_returned_voluntarily'
    # if it's been a while since this business's last logged event. See
    # app/analytics.py for the heuristic (there's no real "session" concept
    # in this app to key off instead).
    analytics.maybe_log_voluntary_return(biz_id)

    # --- A message type we genuinely can't process (voice note, video,
    # document, sticker, location, contact card, ...) -- acknowledge
    # instead of silently dropping it. See app/whatsapp/webhook.py's
    # parse_message(), which used to return None here (total silence, the
    # same failure mode the "uploaded image with no caption" bug had).
    if msg.type == "unsupported":
        await send_text(msg.sender, "I can only understand text messages and photos right now 🙏 Could you try one of those?")
        return

    if intent == "GREETING":
        # Personal, not generic -- addresses them by name (whatever's on
        # file from onboarding) so a returning client feels like they're
        # picking a conversation back up with someone who knows them, not
        # re-introducing themselves to a form. Falls back to the old
        # generic line only if no name was ever captured.
        greeting = (
            f"Hey {display_name}! How's it going? What do you want me to build today? 💡"
            if display_name else "Hey! Want today's post? I've got an idea. 💡"
        )
        await send_text(msg.sender, greeting)
        return

    if intent == "IDENTITY_QUESTION":
        with get_session() as db:
            biz = db.query(Business).filter(Business.id == biz_id).first()
            language = biz.preferred_language or "en"
        reply = await i18n.t("identity_disclosure", language, persona.DISCLOSURE_TEXT)
        await send_text(msg.sender, reply)
        return

    if intent == "GLOBAL_COMMAND":
        command = classification.get("command")
        if command in ("credits", "balance"):
            bal = credits.get_balance(biz_id)
            await send_text(msg.sender, f"💳 You have {bal} credits remaining.")
            return
        if command == "topup":
            await payments.send_topup_options(biz_id, msg.sender)
            return
        if command == "history":
            from app.history import send_recent_generations
            await send_recent_generations(biz_id, msg.sender)
            return
        if command == "connect_instagram":
            from app.instagram_insights_oauth import send_connect_link
            await send_connect_link(biz_id, msg.sender)
            return
        # Classifier said GLOBAL_COMMAND but didn't name which one --
        # fail safe by falling through to the normal pipeline below
        # rather than doing nothing.

    # --- Carousel request: kicks off the count/content negotiation ---
    # Never routed through the normal concept-proposal/revision logic below.
    if intent == "CAROUSEL_REQUEST":
        from app.engine import carousel
        await carousel.start(biz_id, msg)
        return

    # --- Client is declaring an attached image as their logo, to be saved
    # and remembered -- not a photo to edit or post as-is. See
    # app/engine/logo_capture.py. Checked before the "photo with no
    # caption" and generate() branches below, since a captioned "this is
    # my logo" would otherwise be treated as an edit instruction.
    if intent == "LOGO_UPLOAD":
        from app.engine import logo_capture
        await logo_capture.handle(biz_id, msg)
        return

    # --- An uploaded photo with no caption -- ask what to do with it
    # instead of silently dropping it or guessing. A photo WITH a caption
    # falls through to generate() below as before, where the caption is
    # used as the instruction. See app/engine/image_intent.py.
    if msg.type == "image" and msg.media_id and not (msg.text and msg.text.strip()):
        from app.engine import image_intent
        await image_intent.start(biz_id, msg)
        return

    # --- Credit check before anything that costs money ---
    if not allowlist.has_unlimited_access(msg.sender) and credits.get_balance(biz_id) < 1:
        await payments.send_topup_options(
            biz_id, msg.sender,
            prefix="You're out of credits! 🙏 "
        )
        return

    # --- The main event: generate or revise a creative ---
    from app.engine.orchestrator import generate
    await generate(biz_id, msg)
