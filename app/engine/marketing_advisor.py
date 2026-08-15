"""
Sakshi as a marketing consultant, not just an image/carousel executor
(Priority 5 of the Aug 2026 consolidated fix list). Handles marketing and
business-growth questions -- content strategy, pricing, positioning,
timing, competitor/market research -- that fall outside the normal
GENERATE/REVISE/carousel flow, with a hard boundary: anything outside
marketing/business growth FOR THIS CLIENT gets redirected back to
Sakshi's actual job, not answered as a general-purpose chatbot.

Uses Anthropic's server-side web_search tool (declared directly on the
Claude call, no separate provider account or API key -- see
app/anthropic_client.py) so market/competitor answers reflect CURRENT
information instead of stale training knowledge -- the whole point of a
question like "what should I charge compared to competitors right now."
Capped at MAX_SEARCHES_PER_QUESTION per question to bound spend on any
single message; not every marketing question needs a search (e.g. "how
should I structure my Diwali offer" doesn't), so the model decides
per-question whether to search at all.

Called from orchestrator.generate()'s QUESTION/OTHER branch -- after the
identity-question and stated-name checks, before falling back to the
generic "try something like..." menu. Two Claude calls when a message
lands here as MARKETING (classify, then answer); one when it's OFF_TOPIC
or UNCLEAR. Fail-safe: any failure in classify_scope() returns "UNCLEAR",
which just falls through to the existing generic menu -- never blocks a
reply.
"""
import json
import logging

from app.config import settings
from app.engine.context import BusinessContext

logger = logging.getLogger("socioburp.engine.marketing_advisor")

from app.anthropic_client import create_message
from app.json_extract import extract_json_text

MAX_SEARCHES_PER_QUESTION = 3

SCOPE_SYSTEM_PROMPT = """A client is messaging Sakshi, an AI creative and
marketing partner for their small business, on WhatsApp. Classify their
message:

- MARKETING: a genuine marketing, growth, or business-strategy question
  about THEIR OWN business -- content strategy, pricing, positioning,
  what to post and when, competitor or market questions, promotion ideas,
  audience targeting. This is Sakshi's job to answer directly, in text,
  no image involved.
- OFF_TOPIC: clearly NOT about marketing or growing their business --
  general trivia, personal advice unrelated to their business, requests
  to do something outside a creative/marketing partner's job (coding
  help, writing an essay, etc).
- UNCLEAR: casual chat, thanks, small talk, or genuinely ambiguous --
  neither a clear marketing question nor clearly off-topic.

Reply with JSON only, no other text: {"scope": "MARKETING"|"OFF_TOPIC"|"UNCLEAR"}"""

ANSWER_SYSTEM_PROMPT = """You are Sakshi, an AI creative and marketing
partner for Indian small businesses, replying on WhatsApp. A client just
asked a marketing/business-growth question -- answer it directly, like a
sharp, experienced marketing consultant would: practical, specific to
their business, no fluff.

Business profile:
{profile}

Use the web_search tool when the answer depends on current information
you can't be confident about from training alone -- competitor pricing,
current trends, recent platform changes, "right now" style questions.
Don't search for questions you can answer well from general marketing
knowledge (e.g. general strategy advice, how to structure an offer).

Keep the reply WhatsApp-length: a few short paragraphs or a tight list,
not an essay. You are still Sakshi in this reply, not a generic search
assistant -- write like yourself.

Hard boundary: you are a marketing/creative partner, not a general-purpose
assistant. If mid-conversation the client drifts to something genuinely
unrelated to their business's marketing or growth, answer this specific
question but don't proactively offer to help with unrelated things."""


async def classify_scope(text: str) -> str:
    """Returns 'MARKETING' | 'OFF_TOPIC' | 'UNCLEAR'. Fails safe to UNCLEAR."""
    if not text or not text.strip():
        return "UNCLEAR"
    try:
        response = await create_message(
            model=settings.CLAUDE_INTENT_MODEL,
            max_tokens=50,
            system=SCOPE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        out = response.content[0].text.strip()
        out = extract_json_text(out)
        parsed = json.loads(out)
        scope = parsed.get("scope")
        if scope not in ("MARKETING", "OFF_TOPIC", "UNCLEAR"):
            raise ValueError(f"Unexpected scope value: {scope}")
        return scope
    except Exception:
        logger.exception("Marketing-scope classification failed for %r — falling back to UNCLEAR", text)
        return "UNCLEAR"


async def answer(ctx: BusinessContext, text: str) -> str:
    """
    Returns a client-ready reply string. On any failure, returns a plain
    apology rather than raising -- a Claude/web-search hiccup here must
    never leave the client's message unanswered.
    """
    profile_lines = [f"Business name: {ctx.name or 'Unknown'}", f"Industry: {ctx.industry or 'Unknown'}"]
    if ctx.target_audience:
        profile_lines.append(f"Target audience: {ctx.target_audience}")
    if ctx.tone:
        profile_lines.append(f"Brand tone: {ctx.tone}")

    try:
        response = await create_message(
            model=settings.CLAUDE_MARKETING_MODEL,
            max_tokens=1024,
            system=ANSWER_SYSTEM_PROMPT.format(profile="\n".join(profile_lines)),
            tools=[{
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": MAX_SEARCHES_PER_QUESTION,
            }],
            messages=[{"role": "user", "content": text}],
        )
        reply = "\n".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()
        return reply or "Hmm, I couldn't quite put together an answer there 🙏 Could you rephrase the question?"
    except Exception:
        logger.exception("Marketing-question answer failed for %r", text)
        return "Sorry, I hit a snag answering that 🙏 Could you try asking again?"


def off_topic_redirect(ctx: BusinessContext) -> str:
    business = ctx.name or "your business"
    return (
        f"That's outside what I do here 🙏 I'm your creative and marketing partner for {business} — "
        "happy to help with content ideas, pricing, promos, or what's working for businesses like "
        "yours instead!"
    )
