"""
WhatsApp Q&A flow that gets SocioBurp partner ("agency") access to a
business's OWN Meta ad account -- the prerequisite for the ads engine's
future campaign-building work. Every campaign that engine eventually
builds is meant to run, and be billed, against the CLIENT's own ad
account (Business.meta_ad_account_id), never a shared central SocioBurp
account -- see app/models.py's Business fields and
app/engine/meta_partner_access.py, which does the actual Marketing API
calls this flow drives.

Deliberately NOT wired into app/router.py or any campaign-building code
yet -- there is no campaign proposal builder in this codebase to gate.
This module (the conversational flow) and has_ad_account_access() below
(the reusable gate) are prep work, ready for whenever that engine lands.
Wiring it in at that point means: (a) calling start() from wherever a
campaign is first requested if has_ad_account_access() is False instead
of building the proposal, and (b) adding a
`if convo.pending_ad_account_connect: ...` branch to router.py's
dispatch, the same way it already branches on pending_carousel /
pending_image_intent.

State machine (ConversationState.pending_ad_account_connect, JSON-in-Text,
same pattern as pending_carousel/pending_image_intent):

  (start) -- "do you already have a Meta Business Manager + ad account?"
    -> awaiting_has_account
         [No]  -> explain business.facebook.com + offer a manual walkthrough.
                  Terminal -- partner_access_status stays 'not_connected'.
                  We never attempt to create a Business Manager/ad account
                  on the client's behalf.
         [Yes] -> awaiting_ad_account_id -- ask for their Ad Account ID
                    -> awaiting_business_manager_id -- ask for their
                       Business Manager ID (optional, "skip" accepted)
                         -> sends the partner/agency request via
                            meta_partner_access.request_partner_access(),
                            stores meta_ad_account_id (+ BM id if given),
                            sets partner_access_status='pending_approval',
                            tells the client explicitly where to approve
                            it (Business Settings -> Partners)
                            -> awaiting_approval_confirmation

  awaiting_approval_confirmation -- any reply here is treated as "I
    approved it, please check" and re-verifies via
    meta_partner_access.check_partner_access_status() BEFORE ever setting
    partner_access_status='granted'. The client's own word that they
    approved it is never sufficient by itself -- if Meta's own response
    doesn't say CONFIRMED yet, this stays pending and says so plainly.
"""
import json
import logging
import uuid

from app.db import get_session
from app.models import Business, ConversationState
from app.schemas import IncomingMessage
from app.whatsapp.client import send_text, send_buttons
from app.engine import meta_partner_access

logger = logging.getLogger("socioburp.engine.ad_account_connect")

BUTTON_HAS_ACCOUNT_YES = "adacct_has_yes"
BUTTON_HAS_ACCOUNT_NO = "adacct_has_no"


def has_ad_account_access(business: Business) -> bool:
    """
    Reusable gate: True only once partner access has actually been
    CONFIRMED via the Marketing API (see meta_partner_access.py), never
    just requested. Standalone -- not called from anywhere in this
    codebase yet; see this module's docstring.
    """
    return business.partner_access_status == "granted"


def _save_pending(business_id: uuid.UUID, pending: dict):
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        if convo is None:
            convo = ConversationState(business_id=business_id)
            db.add(convo)
        convo.pending_ad_account_connect = json.dumps(pending)


def _clear_pending(business_id: uuid.UUID):
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        if convo:
            convo.pending_ad_account_connect = None


async def start(business_id: uuid.UUID, phone: str):
    """
    Entry point -- call this wherever a campaign is about to be proposed
    and has_ad_account_access() is False (not wired anywhere yet, see
    module docstring).
    """
    _save_pending(business_id, {"stage": "awaiting_has_account"})
    await send_buttons(
        phone,
        "Before I can build ad campaigns for you, I need access to your own Meta ad account "
        "(never a shared one). Do you already have a Meta Business Manager and ad account set up?",
        [
            (BUTTON_HAS_ACCOUNT_YES, "Yes, I have one"),
            (BUTTON_HAS_ACCOUNT_NO, "Not yet"),
        ],
    )


async def advance(business_id: uuid.UUID, msg: IncomingMessage, pending_raw: str):
    """Called for every message while this negotiation is in progress."""
    phone = msg.sender
    pending = json.loads(pending_raw)
    stage = pending.get("stage")

    if stage == "awaiting_has_account":
        if msg.button_id == BUTTON_HAS_ACCOUNT_NO:
            _clear_pending(business_id)
            await send_text(
                phone,
                "No problem! You'll first need to create a Meta Business Manager and ad account — "
                "you can do that at business.facebook.com. If you'd like, our team can walk you "
                "through it the first time. Just let me know once it's set up and we'll connect it.",
            )
            return

        if msg.button_id == BUTTON_HAS_ACCOUNT_YES:
            pending["stage"] = "awaiting_ad_account_id"
            _save_pending(business_id, pending)
            await send_text(
                phone,
                "Great — what's your Meta Ad Account ID? (You'll find it in Meta Ads Manager, "
                "usually a string of digits, sometimes shown as \"act_123456789\")",
            )
            return

        _save_pending(business_id, pending)
        await send_text(phone, "Just tap one of the two options above 🙂")
        return

    if stage == "awaiting_ad_account_id":
        raw = (msg.text or "").strip()
        ad_account_id = meta_partner_access.normalize_ad_account_id(raw) if raw else None
        if not ad_account_id:
            _save_pending(business_id, pending)
            await send_text(phone, "That doesn't look like a valid Ad Account ID — could you double check and resend it?")
            return

        pending["ad_account_id"] = ad_account_id
        pending["stage"] = "awaiting_business_manager_id"
        _save_pending(business_id, pending)
        await send_text(
            phone,
            "Got it. If you know your Business Manager ID, share that too — otherwise just reply \"skip\".",
        )
        return

    if stage == "awaiting_business_manager_id":
        raw = (msg.text or "").strip()
        business_manager_id = None if raw.lower() in ("skip", "no", "don't know", "dont know", "") else raw

        ad_account_id = pending.get("ad_account_id")
        sent = await meta_partner_access.request_partner_access(ad_account_id)
        if not sent:
            _clear_pending(business_id)
            await send_text(
                phone,
                "Something went wrong sending the access request on our end 🙏 "
                "Please try again in a bit, or reach out to our team.",
            )
            return

        with get_session() as db:
            biz = db.query(Business).filter(Business.id == business_id).first()
            biz.meta_ad_account_id = ad_account_id
            biz.meta_business_manager_id = business_manager_id
            biz.partner_access_status = "pending_approval"

        pending["stage"] = "awaiting_approval_confirmation"
        _save_pending(business_id, pending)
        await send_text(
            phone,
            "I've sent a request — you'll need to approve it in your Meta Business Manager under "
            "Business Settings → Partners, then let me know once it's done.",
        )
        return

    if stage == "awaiting_approval_confirmation":
        ad_account_id = pending.get("ad_account_id")
        status = await meta_partner_access.check_partner_access_status(ad_account_id)

        if status == "CONFIRMED":
            with get_session() as db:
                biz = db.query(Business).filter(Business.id == business_id).first()
                biz.partner_access_status = "granted"
            _clear_pending(business_id)
            await send_text(phone, "Confirmed ✅ Your ad account is connected — I can start building campaigns for you now.")
            return

        _save_pending(business_id, pending)
        await send_text(
            phone,
            "I don't see it as approved on Meta's side yet 🙏 Please check Business Settings → Partners "
            "in your Meta Business Manager, approve the request there, then message me again.",
        )
        return

    logger.warning("Unknown pending_ad_account_connect stage %r for business=%s — clearing", stage, business_id)
    _clear_pending(business_id)
