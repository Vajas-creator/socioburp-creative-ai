"""
Activation-funnel event logging. Pure instrumentation -- no behavior
change anywhere it's called from, and a failure here must never break a
real user-facing flow (fails safe: logs and swallows, same discipline as
the AI-call modules' try/except pattern).

Four events, per business:
  signup                    -- a brand-new Business row was just created
                                (app/router.py's get_or_create_business()).
  onboarding_completed       -- onboarding_state just transitioned to "done"
                                (app/onboarding.py).
  first_creative_approved    -- the FIRST time this business ever gets a
                                real accept signal (app/engine/learning.py's
                                record_accepted_direction() firing its
                                'recorded' outcome) -- not just "a creative
                                was delivered", an actual acceptance signal
                                (moving on without revising, or tapping
                                "Post to Instagram"). Logged once per
                                business, not on every subsequent accept.
  user_returned_voluntarily  -- a message arrived from an already-onboarded
                                business that hasn't been heard from in a
                                while. There's no literal "session" concept
                                in this app, so this is a heuristic: more
                                than RETURN_GAP_HOURS since their last
                                logged event of any kind. See
                                app/router.py's _process_message().
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from app.db import get_session
from app.models import AnalyticsEvent

logger = logging.getLogger("socioburp.analytics")

# How long since a business's last logged event before a new message counts
# as a voluntary return rather than a continuation of the same exchange.
RETURN_GAP_HOURS = 12


def log_event(business_id: uuid.UUID, event_type: str, **metadata):
    """
    Fire-and-forget style: call this inline, don't await-block real flows
    on it mattering. Any failure (DB hiccup, etc.) is logged and swallowed
    -- instrumentation must never be the reason a real message fails to
    process.
    """
    try:
        with get_session() as db:
            db.add(AnalyticsEvent(
                business_id=business_id,
                event_type=event_type,
                event_metadata=metadata or None,
            ))
        logger.info("Analytics event: business=%s type=%s %s", business_id, event_type, metadata or "")
    except Exception:
        logger.exception("Failed to log analytics event type=%s for business=%s", event_type, business_id)


def maybe_log_voluntary_return(business_id: uuid.UUID):
    """
    Call on every message from an already-onboarded business (see
    app/router.py). Logs 'user_returned_voluntarily' if -- and only if --
    more than RETURN_GAP_HOURS have passed since this business's last
    logged event of any kind. Safe to call on every message: cheap read,
    and does nothing (no event, no side effect) on a false result.
    """
    try:
        with get_session() as db:
            last_event = (
                db.query(AnalyticsEvent)
                .filter(AnalyticsEvent.business_id == business_id)
                .order_by(AnalyticsEvent.created_at.desc())
                .first()
            )
            if last_event is None:
                return  # no prior event at all -- nothing to "return" from yet
            last_seen_at = last_event.created_at

        if last_seen_at.tzinfo is None:
            last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
        gap = datetime.now(timezone.utc) - last_seen_at
        if gap >= timedelta(hours=RETURN_GAP_HOURS):
            log_event(business_id, "user_returned_voluntarily", gap_hours=round(gap.total_seconds() / 3600, 1))
    except Exception:
        logger.exception("Failed to evaluate voluntary-return check for business=%s", business_id)
