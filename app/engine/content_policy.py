"""
Content-policy guardrail, checked before any paid generation runs. Sakshi
must never produce false claims, restricted-category content, or Meta
advertising-policy violations, even if a client explicitly asks -- see
the Aug 2026 consolidated fix list, Priority 7. Runs once per generation
request, against the client's own raw text (not a Claude-paraphrased
brief), since that's the actual point where intent to violate a policy
would show up.

Deliberately narrow and conservative: flags only reasonably clear
violations (fabricated certifications/awards, medical/health treatment
claims, financial guarantees, restricted categories -- weapons, drugs,
adult content, hate/discriminatory content), not stylistic judgment
calls -- ordinary small-business marketing enthusiasm must never get
blocked. Fails open (allowed) on a classifier error: blocking every
generation because one moderation call failed would be a worse failure
mode than occasionally missing a genuine violation, and this is
defense-in-depth, not the only layer -- prompt_builder.py's own system
prompt also carries a baseline honesty/compliance rule.
"""
import json
import logging

from app.config import settings

logger = logging.getLogger("socioburp.engine.content_policy")

from app.anthropic_client import create_message

SYSTEM_PROMPT = """You review requests to an AI marketing-creative
generator for small businesses, before any image gets made. Block a
request ONLY if it clearly asks for one of these:

- A false or unverifiable claim presented as fact: a specific
  certification, award, ranking, or endorsement the business has not
  actually stated they have (e.g. "add an ISO certified badge", "say
  we're the #1 rated in the city") -- generic marketing enthusiasm
  ("best in town!", "amazing quality") is fine, a SPECIFIC fabricated
  credential/statistic is not.
- A medical, health, or treatment claim (curing/treating/preventing a
  disease or condition).
- A financial guarantee or "guaranteed returns" claim.
- A restricted category: weapons/firearms, illegal drugs, adult/sexual
  content, hate speech or discriminatory content, content targeting
  minors inappropriately.

Almost every real request from a small business (offers, festival posts,
product photos, discounts, general marketing copy) is fine -- when in
doubt, ALLOW. Only block a clear, specific match to the categories above.

Reply with JSON only, no other text:
{"allowed": true} or {"allowed": false, "reason": "one short client-facing sentence explaining what can't be included, said kindly"}"""


async def check(text: str) -> dict:
    """
    Returns {"allowed": bool, "reason": str|None}. Fails open (allowed)
    on any classifier error -- see module docstring.
    """
    if not text or not text.strip():
        return {"allowed": True, "reason": None}
    try:
        response = await create_message(
            model=settings.CLAUDE_INTENT_MODEL,
            max_tokens=150,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        out = response.content[0].text.strip()
        if out.startswith("```"):
            out = out.strip("`").removeprefix("json").strip()
        parsed = json.loads(out)
        allowed = parsed.get("allowed")
        if not isinstance(allowed, bool):
            raise ValueError(f"Unexpected 'allowed' value: {allowed}")
        return {"allowed": allowed, "reason": parsed.get("reason") if not allowed else None}
    except Exception:
        logger.exception("Content-policy check failed for %r — failing open (allowed)", text)
        return {"allowed": True, "reason": None}
