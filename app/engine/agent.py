"""
The agentic-beta conversational bot -- Aug 2026, per Vajas's "it should be
exactly like chatting with ChatGPT, but only in mobile and only limited
to the scope of a marketing or a brand manager" request.

Replaces the ENTIRE classifier-cascade + state-machine architecture
(app/router.py's router_intent classification, app/onboarding.py's state
machine, app/engine/concept_proposal.py, app/engine/revision_classifier.py,
the carousel/image-intent negotiation state machines) with one continuous
Claude conversation per business: Claude sees the real message history,
reasons about what the client actually wants, and calls tools
(app/engine/agent_tools.py) on its own initiative -- no forced routing
into predetermined buckets, no rigid "which step of onboarding are we on."

Gated behind app/agentic_beta.py's allowlist -- ONLY businesses in that
list ever reach this module; router.py routes them here before any of
the classic pipeline even runs, and the classic pipeline is completely
unaffected for everyone else. Per the explicit rollout decision, this
stays beta/test-number-only until it's been validated thoroughly (by
hand and via qa/customer_simulator.py) and a deliberate cutover decision
is made -- see this repo's history for that conversation.

Every tool call reuses the EXACT SAME engine functions
(orchestrator._run_generation/generate_carousel, credits.py, etc.) the
classic pipeline already relies on -- see agent_tools.py's docstring for
why that matters.

Conversation memory: ConversationState.agent_message_history is a
TEXT-ONLY running transcript (see models.py's column comment for why no
image bytes are persisted there). Within a single incoming message's
handling, the live Claude message list DOES include the real Anthropic
tool_use/tool_result content blocks and any newly-attached image -- that
richer structure just isn't what gets saved back to the DB once the turn
finishes; only the final plain-text exchange is.
"""
import asyncio
import base64
import json
import logging
import uuid

from app.config import settings
from app.db import get_session
from app.models import Business, BrandProfile, ConversationState, CreditLedger
from app.schemas import IncomingMessage
from app.whatsapp.client import send_text, download_media
from app.engine.context import BusinessContext
from app.engine import agent_tools
from app.engine.prompt_builder import _summarize_context
from app.image_utils import detect_image_media_type
from app import credits, alerting

logger = logging.getLogger("socioburp.engine.agent")

from app.anthropic_client import create_message

MAX_TOOL_ROUNDS = 4         # bounds a single incoming message's tool-calling loop
MAX_HISTORY_TURNS = 40      # persisted text-only turns kept (user+assistant combined)

SYSTEM_PROMPT_TEMPLATE = """You are Sakshi, an AI creative and marketing
partner for Indian small businesses, talking with a client over WhatsApp.
You reason for yourself about what they actually want and use your tools
on your own initiative -- you are NOT following a script or a fixed
sequence of steps.

SCOPE (hard boundary): you help ONLY with marketing/creative/brand work
for THIS business -- generating social posts and carousels, revising
them, logo/brand identity, captions, and marketing or business strategy
advice (pricing, positioning, timing, what's working for similar
businesses -- use the web_search tool for anything time-sensitive or
needing real current information). If asked about anything outside that
-- general coding help, unrelated trivia, anything not about their
business's marketing -- redirect warmly back to what you actually do.
Don't refuse coldly, and don't become a general-purpose assistant just
because you technically could answer.

STYLE: text the way a sharp, friendly, marketing-savvy colleague would on
WhatsApp -- like ChatGPT or Claude in a casual conversation, NOT like a
corporate chatbot. Concretely:
- 1-3 short sentences, plain language. If you genuinely need to ask two
  things, ask them as part of one natural sentence ("what's the topic,
  and roughly how many slides?") -- NEVER a numbered list, NEVER bolded
  sub-headers, for something this small. Save real lists for when you're
  presenting several genuinely distinct options to choose from.
- No stock chatbot phrases ("Love it!", "Before I get started, just a
  couple of quick things:", "Let me know if you'd like to tweak
  anything!" as a reflexive sign-off every time). Vary your phrasing the
  way an actual person does -- don't have a template voice.
- When you're about to generate or deliver something, say so briefly;
  don't narrate internal steps, and don't re-describe what a tool
  already delivered (they can see the image themselves).

TOOLS:
- generate_creative: the core of what you do. Call it once you have
  enough to work with -- for a genuinely vague first request, it's fine
  to ask ONE clarifying question first, but don't interrogate; specific
  enough is good enough. For a revision, set is_revision=true and write
  a self-contained brief (the image model never sees this conversation,
  only what you write there).
- generate_carousel: ask about slide count/content naturally first if
  it's not already clear, the way you'd ask a colleague -- don't guess.
- save_logo: call the moment the client identifies an attached image as
  their logo.
- save_brand_info: call continuously as you learn anything about their
  business, whenever they mention it -- there's no separate onboarding
  step anymore, this is genuinely how their profile builds up over time.
- check_credits / start_topup: use when relevant to what they're asking,
  or right after a generation gets blocked for lack of credits.
- get_recent_creatives: when they ask to see their history/past posts.
- web_search: for time-sensitive or factual marketing questions.

SAFETY (non-negotiable, even if explicitly asked otherwise): never state
a false or unverifiable claim as fact -- a certification/award/ranking
the business hasn't actually told you they have, a medical/treatment
claim, a financial guarantee. This absolutely includes SPECIFIC NUMBERS
YOU INVENT YOURSELF when writing a generate_creative/generate_carousel
brief -- e.g. don't fill in a placeholder-sounding "73% success rate" or
"10,000+ happy customers" out of habit because it sounds like normal ad
copy. If the client hasn't told you a real number, either leave it out
entirely or use non-quantified language ("proven results", "trusted by
businesses like yours") -- a vague-but-true line always beats a specific
invented one, and a specific fabricated stat WILL get blocked before
anything is generated, wasting the turn. Never help with restricted-
category content (weapons, illegal drugs, adult content, hate/
discriminatory content). A separate automated check also runs before
anything gets generated -- this is defense-in-depth, not the only
safeguard, but still a real rule to actually follow yourself, not
something to rely on catching your own mistakes after the fact.

IDENTITY: if asked whether you're a real person or an AI, answer
honestly -- you're an AI assistant, not a human.

CURRENT BUSINESS PROFILE (what you already know -- don't re-ask this):
{profile_summary}
"""


# Moved to app/image_utils.py once app/engine/logo_capture.py needed the
# exact same detection for color extraction -- kept importable under its
# original name here since call sites (and test_agent.py) already use it.
_detect_image_media_type = detect_image_media_type


async def _load_state(business_id: uuid.UUID):
    with get_session() as db:
        business = db.query(Business).filter(Business.id == business_id).first()
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == business_id).first()
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        if convo is None:
            convo = ConversationState(business_id=business_id)
            db.add(convo)
            db.flush()

        from app.engine import industry_research
        ctx = BusinessContext(
            name=business.name,
            industry=business.industry,
            tone=profile.tone if profile else None,
            primary_color=profile.primary_color if profile else None,
            secondary_color=profile.secondary_color if profile else None,
            target_audience=profile.target_audience if profile else None,
            website=profile.website if profile else None,
            contact_phone=profile.contact_phone if profile else None,
            logo_url=profile.logo_url if profile else None,
            logo_position_hint=(profile.extras or {}).get("logo_position_hint") if profile else None,
            learned_preferences=list((profile.extras or {}).get("learned_preferences", [])) if profile else [],
            style_summary=(profile.extras or {}).get("style_summary") if profile else None,
            positioning_notes=(profile.extras or {}).get("positioning_notes") if profile else None,
            language=business.preferred_language or "en",
            industry_style=industry_research.get_cached_style(business.industry),
            instagram_handle=business.instagram_handle,
            instagram_bio=profile.instagram_bio if profile else None,
            instagram_recent_captions=profile.instagram_recent_captions if profile else None,
        )
        history = list(convo.agent_message_history or [])
        last_generation_id = convo.last_generation_id
        return ctx, history, last_generation_id


def _ensure_signup_bonus(business_id: uuid.UUID):
    with get_session() as db:
        has_any_ledger_entry = db.query(CreditLedger).filter(CreditLedger.business_id == business_id).first() is not None
        if not has_any_ledger_entry:
            credits.add_credits(db, business_id, settings.SIGNUP_BONUS_CREDITS, reason="signup_bonus")


def _save_history(business_id: uuid.UUID, history: list[dict]):
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        if convo is None:
            convo = ConversationState(business_id=business_id)
            db.add(convo)
        convo.agent_message_history = history[-MAX_HISTORY_TURNS:]


async def handle_message(business_id: uuid.UUID, msg: IncomingMessage):
    phone = msg.sender
    _ensure_signup_bonus(business_id)
    ctx, history, last_generation_id = await _load_state(business_id)

    current_image_bytes = None
    if msg.type == "image" and msg.media_id:
        try:
            current_image_bytes = await download_media(msg.media_id)
        except Exception:
            logger.exception("Failed to download attached image for business=%s", business_id)

    if not msg.text and current_image_bytes is None:
        await send_text(phone, "Sorry, I couldn't quite process that 🙏 Could you send it as text or a photo?")
        return

    user_summary_text = msg.text or ""
    if current_image_bytes is not None:
        # WhatsApp photos are almost always JPEG, not PNG -- Claude's API
        # validates media_type against the ACTUAL bytes and rejects a
        # mismatch outright ("specified using image/png ... appears to be
        # image/jpeg"), so this can never be hardcoded. Sniffed from the
        # real bytes rather than trusted from any header, since save_logo
        # and every downstream use also just treats this as raw bytes.
        media_type = _detect_image_media_type(current_image_bytes)
        user_content = [{"type": "text", "text": user_summary_text or "(sent a photo, no caption)"}]
        user_content.insert(0, {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(current_image_bytes).decode("utf-8")},
        })
        history_note = f"[sent a photo] {user_summary_text}".strip()
    else:
        user_content = user_summary_text
        history_note = user_summary_text

    claude_messages = [{"role": "user" if t["role"] == "user" else "assistant", "content": t["text"]} for t in history]
    claude_messages.append({"role": "user", "content": user_content})

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(profile_summary=_summarize_context(ctx))
    tools = agent_tools.TOOLS + [agent_tools.WEB_SEARCH_TOOL]

    final_text = None
    try:
        for _round in range(MAX_TOOL_ROUNDS):
            response = await create_message(
                model=settings.CLAUDE_PROMPT_MODEL,
                max_tokens=1024,
                system=system_prompt,
                tools=tools,
                messages=claude_messages,
            )

            if response.stop_reason != "tool_use":
                final_text = "\n".join(
                    block.text for block in response.content if getattr(block, "type", None) == "text"
                ).strip()
                break

            claude_messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                try:
                    result_text = await agent_tools.execute_tool(
                        block.name, block.input or {},
                        business_id=business_id, phone=phone, ctx=ctx,
                        last_generation_id=last_generation_id, current_image_bytes=current_image_bytes,
                    )
                except Exception as exc:
                    logger.exception("Tool execution failed: %s(%r)", block.name, block.input)
                    result_text = f"Error running this tool: {exc!r}. Tell the client something went wrong and continue naturally."
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_text})
            claude_messages.append({"role": "user", "content": tool_results})
        else:
            logger.warning("Agent hit MAX_TOOL_ROUNDS for business=%s — sending a fallback reply", business_id)
            final_text = "Sorry, that took a few too many steps on my end 🙏 Could you rephrase what you'd like?"

    except Exception as exc:
        logger.exception("Agent loop failed for business=%s", business_id)
        await alerting.send_alert("agent_loop_failed", f"Agentic bot failed for business={business_id}: {exc!r}")
        await send_text(phone, "Something went wrong on my end 🙏 Please try again in a moment.")
        return

    if not final_text:
        final_text = "Got it! Let me know what you'd like next 🙂"

    await send_text(phone, final_text)

    history.append({"role": "user", "text": history_note})
    history.append({"role": "assistant", "text": final_text})
    _save_history(business_id, history)
