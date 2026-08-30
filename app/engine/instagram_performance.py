"""
Plain-text WhatsApp summary of a business's Instagram performance, for the
classic pipeline's "instagram_performance" global command (see
app/router.py). Deliberately NOT part of app/engine/instagram_insights.py
-- that module is a pure read client and explicitly stays out of
formatting/interpretation, per its own docstring ("It does not interpret
them... keeping them out of here so this stays a pure, easily-testable
data-access layer").

Reuses the exact same read functions app/engine/agent_tools.py's
check_instagram_performance tool calls (get_account_insights,
get_recent_media, get_media_insights) -- this is just a different
presentation layer (plain deterministic text, no LLM in the loop, since
the classic pipeline doesn't have one) for the SAME underlying data. See
instagram_insights.get_recent_media()'s docstring for why "most recent
post" is resolved from Meta directly rather than our own Generation rows.

This -- alongside app/engine/instagram_publish.py's classic "post to
instagram" entry point -- exists specifically so a business messaging
through the ordinary (non-agentic-beta) pipeline can actually exercise
the instagram_manage_insights grant end-to-end: before this, the read
client was fully built but never called from anywhere user-facing, which
is a real weakness for Meta's App Review (reviewers want to see the
permission's data rendered, not just the OAuth grant succeeding).
"""
import logging
import uuid

from app.whatsapp.client import send_text
from app.engine import instagram_insights

logger = logging.getLogger("socioburp.engine.instagram_performance")


def _format_metrics(data: list[dict], *, latest_value_only: bool) -> str:
    """
    `data` is the raw Graph API Insights {"data": [...]} list -- each item
    has a `name` and a `values` list. Account-level metrics with
    period="day" can carry more than one day's value; latest_value_only
    picks the last (most recent) one. Media-level (lifetime) metrics only
    ever have one value, so it doesn't matter there.
    """
    parts = []
    for item in data:
        values = item.get("values") or []
        if not values:
            continue
        value = values[-1]["value"] if latest_value_only else values[0]["value"]
        parts.append(f"{item['name'].replace('_', ' ').title()}: {value}")
    return " · ".join(parts)


async def send_performance_summary(business_id: uuid.UUID, phone: str):
    """Called from app/router.py's 'instagram_performance' global command."""
    if not instagram_insights.is_connected(business_id):
        await send_text(
            phone,
            "Your Instagram isn't connected for performance tracking yet 🙏 "
            "Text 'connect instagram' to set it up.",
        )
        return

    account = await instagram_insights.get_account_insights(business_id)
    recent_media = await instagram_insights.get_recent_media(business_id, limit=1)

    lines = ["📊 Your Instagram performance:"]

    if account and account.get("data"):
        summary = _format_metrics(account["data"], latest_value_only=True)
        lines.append(summary or "No account data for this period yet.")
    else:
        lines.append("Account-level data isn't available right now — please try again shortly.")

    media_info = recent_media[0] if recent_media else None
    if media_info:
        media_insights = await instagram_insights.get_media_insights(business_id, media_info["id"])
        if media_insights and media_insights.get("data"):
            summary = _format_metrics(media_insights["data"], latest_value_only=False)
            lines.append(f"\nMost recent post: {summary}")
        else:
            lines.append("\nMost recent post: metrics aren't available right now.")
    elif recent_media == []:
        lines.append("\nNo posts on your account yet.")

    await send_text(phone, "\n".join(lines))
