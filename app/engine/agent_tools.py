"""
Tool definitions + implementations for the agentic-beta conversational
bot (see app/engine/agent.py). Every tool here is a THIN wrapper around
the SAME engine functions the classic pipeline already uses --
orchestrator._run_generation()/generate_carousel(), credits.py,
payments.py, logo_capture.py, history.py -- deliberately, so this new
conversational layer inherits every hard-won safety mechanism (content
policy, credit charging, rate limiting, quality gate + regen, AI-source
metadata, smart logo placement, alerting) for free instead of
re-implementing (and risking re-breaking) any of it. The agent decides
WHEN to call these; what each one actually does on the way to delivering
a creative is completely unchanged from the classic pipeline.
"""
import logging
import uuid

from app.db import get_session
from app.models import Business, BrandProfile, ConversationState
from app import credits, payments, allowlist
from app.engine import image_history
from app.engine.context import BusinessContext

logger = logging.getLogger("socioburp.engine.agent_tools")

MAX_CAROUSEL_SLIDES = 9

TOOLS = [
    {
        "name": "generate_creative",
        "description": (
            "Generate ONE marketing creative (a single image + caption) and deliver it to the "
            "client. Use this for a brand-new creative OR a revision of something already "
            "delivered in this conversation. For a revision, set is_revision=true and write a "
            "self-contained brief that captures both what's being kept and what's changing "
            "(e.g. 'the Diwali sale post from before, but with a warmer gold background and no "
            "discount text on the image') -- the image model itself doesn't see this "
            "conversation, only exactly what you write in `brief`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brief": {"type": "string", "description": "A complete, self-contained description of exactly what the creative should show."},
                "is_revision": {"type": "boolean", "description": "true if this changes/replaces a creative already generated in this conversation, false for a genuinely new one."},
                "reference_hint": {
                    "type": ["string", "null"],
                    "description": "Only if the client is pointing at a SPECIFIC past image, not just 'the last one' -- e.g. 'the second one' or 'the logo photo'. Null otherwise.",
                },
            },
            "required": ["brief", "is_revision"],
        },
    },
    {
        "name": "generate_carousel",
        "description": (
            "Generate a multi-image Instagram carousel -- multiple genuinely separate images "
            "posted together as one set. Only call this once you actually know how many slides "
            "and roughly what each should show -- ask the client conversationally first if "
            "that's not yet clear, the same way you'd naturally ask a colleague, don't guess."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slide_briefs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "One complete, self-contained brief per slide, in order. 1-9 items.",
                },
            },
            "required": ["slide_briefs"],
        },
    },
    {
        "name": "save_logo",
        "description": (
            "Save an image the client just attached as their business logo, for smart automatic "
            "placement on every future creative. Only call this when the client has clearly "
            "indicated the attached image IS their logo, not a product photo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "position_hint": {
                    "type": ["string", "null"],
                    "description": "Where they'd like it placed, in their own words, e.g. 'bottom right' or 'somewhere subtle, not on the food'. Null if they didn't say.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "check_credits",
        "description": "Look up the client's current credit balance.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "start_topup",
        "description": "Send the client the credit top-up purchase options. Use when they want to buy more credits, or right after a generation was blocked for lack of credits.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "save_brand_info",
        "description": (
            "Persist something you've learned about the client's business during natural "
            "conversation. Call this whenever they tell you any of this, even in passing -- "
            "there's no separate 'onboarding' step anymore, this IS how their profile gets "
            "built up over time. Only include fields you actually learned something about;  "
            "omit (don't null out) anything not mentioned this turn."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "industry": {"type": "string"},
                "primary_color": {"type": "string", "description": "Hex code, e.g. #E63946, if mentioned or clearly implied."},
                "secondary_color": {"type": "string"},
                "tone": {"type": "string", "description": "e.g. premium, friendly, bold, minimal"},
                "target_audience": {"type": "string"},
                "positioning_notes": {"type": "string", "description": "Price range, style dos-and-don'ts, or anything else worth remembering verbatim."},
                "website": {"type": "string"},
                "contact_phone": {"type": "string"},
            },
        },
    },
    {
        "name": "get_recent_creatives",
        "description": "Show the client their recently generated creatives. Use when they ask to see their history or past posts.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}


async def _tool_generate_creative(business_id: uuid.UUID, phone: str, ctx: BusinessContext, last_generation_id, args: dict) -> str:
    from app.engine.orchestrator import _run_generation, _check_rate_limit

    unlimited = allowlist.has_unlimited_access(phone)
    if not unlimited and credits.get_balance(business_id) < 1:
        await payments.send_topup_options(business_id, phone, prefix="You're out of credits! 🙏 ")
        return "Blocked: out of credits. Topup options were sent to the client -- tell them briefly, don't repeat the options yourself."

    with get_session() as db:
        within_limit = _check_rate_limit(db, business_id)
    if not within_limit:
        return "Blocked: hourly generation rate limit reached. Tell the client to try again in a bit."

    brief = args["brief"]
    is_revision = bool(args.get("is_revision"))
    reference_hint = args.get("reference_hint")

    reference_image = None
    if is_revision and reference_hint:
        referenced = await image_history.resolve_reference(business_id, reference_hint)
        if referenced:
            import httpx
            try:
                async with httpx.AsyncClient(timeout=15.0) as http_client:
                    resp = await http_client.get(referenced["url"])
                if resp.status_code == 200:
                    reference_image = resp.content
            except Exception:
                logger.exception("Failed to fetch resolved reference image for business=%s", business_id)

    await _run_generation(
        business_id, phone, ctx, brief, brief,
        last_generation_id=last_generation_id if is_revision else None,
        is_revision=is_revision, trigger_source="agentic", reference_image=reference_image,
    )
    return "Delivered (or a clear error was already sent to the client if something went wrong -- don't re-describe success or failure, the image/message speaks for itself; just continue the conversation naturally)."


async def _tool_generate_carousel(business_id: uuid.UUID, phone: str, ctx: BusinessContext, last_generation_id, args: dict) -> str:
    from app.engine.orchestrator import generate_carousel

    slide_briefs = args.get("slide_briefs") or []
    if not slide_briefs:
        return "Error: slide_briefs was empty. Ask the client what each slide should show, then call this again."
    slide_briefs = slide_briefs[:MAX_CAROUSEL_SLIDES]
    credit_cost = len(slide_briefs)

    unlimited = allowlist.has_unlimited_access(phone)
    if not unlimited and credits.get_balance(business_id) < credit_cost:
        await payments.send_topup_options(
            business_id, phone,
            prefix=f"A {credit_cost}-image carousel uses {credit_cost} credits and you don't have enough right now 🙏 ",
        )
        return "Blocked: out of credits for this carousel size. Topup options were sent -- tell the client briefly."

    await generate_carousel(business_id, phone, ctx, slide_briefs, user_message=" | ".join(slide_briefs), last_generation_id=last_generation_id)
    return "Delivered (or a clear error was already sent if something went wrong) -- don't re-describe success/failure, just continue naturally."


async def _tool_save_logo(business_id: uuid.UUID, phone: str, current_image_bytes: bytes | None, args: dict) -> str:
    if current_image_bytes is None:
        return "Error: no image was attached to the client's most recent message. Ask them to send the logo image."

    from app.storage import upload_logo
    import asyncio
    try:
        logo_url = await asyncio.to_thread(upload_logo, business_id, current_image_bytes)
    except Exception:
        logger.exception("Failed to upload logo for business=%s", business_id)
        return "Error: the upload failed. Tell the client to try sending it again."

    with get_session() as db:
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == business_id).first()
        if profile is None:
            profile = BrandProfile(business_id=business_id)
            db.add(profile)
        profile.logo_url = logo_url
        hint = args.get("position_hint")
        if hint:
            extras = dict(profile.extras or {})
            extras["logo_position_hint"] = hint[:200]
            profile.extras = extras

    return "Saved. It'll be placed thoughtfully (not just a fixed corner) on every future creative."


def _tool_check_credits(business_id: uuid.UUID) -> str:
    balance = credits.get_balance(business_id)
    return f"Current balance: {balance} credits."


async def _tool_start_topup(business_id: uuid.UUID, phone: str) -> str:
    await payments.send_topup_options(business_id, phone)
    return "Topup options were sent to the client directly -- don't repeat them yourself, just acknowledge naturally if needed."


def _tool_save_brand_info(business_id: uuid.UUID, args: dict) -> str:
    saved = []
    with get_session() as db:
        biz = db.query(Business).filter(Business.id == business_id).first()
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == business_id).first()
        if profile is None:
            profile = BrandProfile(business_id=business_id)
            db.add(profile)

        if args.get("name"):
            biz.name = args["name"]
            saved.append("name")
        if args.get("industry"):
            biz.industry = args["industry"]
            saved.append("industry")
        if args.get("primary_color"):
            profile.primary_color = args["primary_color"]
            saved.append("primary_color")
        if args.get("secondary_color"):
            profile.secondary_color = args["secondary_color"]
            saved.append("secondary_color")
        if args.get("tone"):
            profile.tone = args["tone"]
            saved.append("tone")
        if args.get("target_audience"):
            profile.target_audience = args["target_audience"]
            saved.append("target_audience")
        if args.get("website"):
            profile.website = args["website"]
            saved.append("website")
        if args.get("contact_phone"):
            profile.contact_phone = args["contact_phone"]
            saved.append("contact_phone")
        if args.get("positioning_notes"):
            extras = dict(profile.extras or {})
            extras["positioning_notes"] = args["positioning_notes"][:1000]
            profile.extras = extras
            saved.append("positioning_notes")

    return f"Saved: {', '.join(saved) if saved else '(nothing new)'}."


async def _tool_get_recent_creatives(business_id: uuid.UUID, phone: str) -> str:
    from app.history import send_recent_generations
    await send_recent_generations(business_id, phone)
    return "Recent creatives were sent directly to the client -- don't re-list them yourself."


async def execute_tool(
    name: str, args: dict, *, business_id: uuid.UUID, phone: str, ctx: BusinessContext,
    last_generation_id, current_image_bytes: bytes | None,
) -> str:
    """
    Dispatches a single tool call. Every branch is defensively wrapped by
    the caller (app/engine/agent.py) too -- this never raises past this
    function on a KNOWN tool; an unknown tool name is itself just
    returned as a tool_result error string for Claude to see and recover
    from, not a crash.
    """
    if name == "generate_creative":
        return await _tool_generate_creative(business_id, phone, ctx, last_generation_id, args)
    if name == "generate_carousel":
        return await _tool_generate_carousel(business_id, phone, ctx, last_generation_id, args)
    if name == "save_logo":
        return await _tool_save_logo(business_id, phone, current_image_bytes, args)
    if name == "check_credits":
        return _tool_check_credits(business_id)
    if name == "start_topup":
        return await _tool_start_topup(business_id, phone)
    if name == "save_brand_info":
        return _tool_save_brand_info(business_id, args)
    if name == "get_recent_creatives":
        return await _tool_get_recent_creatives(business_id, phone)
    return f"Error: unknown tool '{name}'."
