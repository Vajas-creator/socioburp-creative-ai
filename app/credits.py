"""
Credits are append-only. Balance = SUM(delta) for a business, computed on
read. Never store a running balance column — it's a source of bugs under
concurrent writes. The ledger IS the source of truth and is fully auditable.

Regen budget tracking (added alongside the credit ledger, not part of it):
a "credit" is priced assuming a single image-gen pass. The quality-check
regen in orchestrator.py doubles the real cost of that one credit when it
fires. Rather than tracking every raw API call, we track a simpler earned
allowance: every time credits are purchased (signup bonus or topup), the
business earns floor(amount / REGEN_BUDGET_RATIO) quality-check regens —
e.g. a 20-credit batch earns 6 regens, matching "roughly one regen per 3
generations" (Vajas, Aug 2026). This is deliberately based on credits
PURCHASED, not credits already used — an allowance available immediately
from a fresh batch, not accrued one generation at a time (which would
starve regens for the first few generations of every batch). Allowance
accumulates across multiple top-ups rather than resetting, so unused
allowance from an earlier purchase is never lost.
"""
import logging
import uuid

from sqlalchemy import func

from app.db import get_session
from app.models import CreditLedger, Business

logger = logging.getLogger("socioburp.credits")

# Credits purchased per quality-check regen earned. 3 -> a 20-credit batch
# earns floor(20/3) = 6 regens, i.e. roughly one regen per 3 generations.
REGEN_BUDGET_RATIO = 3


def get_balance(business_id: uuid.UUID) -> int:
    with get_session() as db:
        total = (
            db.query(func.coalesce(func.sum(CreditLedger.delta), 0))
            .filter(CreditLedger.business_id == business_id)
            .scalar()
        )
        return int(total)


def add_credits(db, business_id: uuid.UUID, amount: int, reason: str, ref_id=None):
    """
    Call this WITH an existing session (db) when it needs to be part of a
    larger transaction (e.g. onboarding completion, or charging alongside
    marking a generation done). Pass amount as negative to deduct.
    """
    entry = CreditLedger(business_id=business_id, delta=amount, reason=reason, ref_id=ref_id)
    db.add(entry)
    logger.info("Credit ledger entry: business=%s delta=%s reason=%s", business_id, amount, reason)

    if amount > 0:
        # A real purchase (signup bonus or topup) earns regen allowance —
        # added to whatever's left, never reset. See module docstring.
        earned = amount // REGEN_BUDGET_RATIO
        if earned > 0:
            biz = db.query(Business).filter(Business.id == business_id).first()
            if biz:
                biz.regen_allowance_this_cycle = (biz.regen_allowance_this_cycle or 0) + earned


def charge_for_generation(business_id: uuid.UUID, generation_id: uuid.UUID, amount: int = 1):
    """
    Standalone charge — used by the engine right after marking a generation
    'done'. Kept as its own session/transaction, separate from add_credits(),
    for the common case where the caller doesn't already have a session open.
    """
    with get_session() as db:
        add_credits(db, business_id, -amount, reason="generation", ref_id=str(generation_id))


def regen_within_budget(business_id: uuid.UUID) -> bool:
    """
    Returns True if this business still has quality-check regen allowance
    left (regens_used_this_cycle < regen_allowance_this_cycle). False means
    the regen should be skipped and the generation blocked rather than
    delivered — see orchestrator._run_generation.
    """
    with get_session() as db:
        biz = db.query(Business).filter(Business.id == business_id).first()
        if biz is None:
            return True  # fail open on a lookup miss — shouldn't happen in practice
        return (biz.regens_used_this_cycle or 0) < (biz.regen_allowance_this_cycle or 0)


def record_regen_used(business_id: uuid.UUID):
    """Call once, right when a quality-check regen is actually performed."""
    with get_session() as db:
        biz = db.query(Business).filter(Business.id == business_id).first()
        if biz:
            biz.regens_used_this_cycle = (biz.regens_used_this_cycle or 0) + 1
