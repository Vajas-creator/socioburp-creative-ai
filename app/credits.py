"""
Credits are append-only. Balance = SUM(delta) for a business, computed on
read. Never store a running balance column — it's a source of bugs under
concurrent writes. The ledger IS the source of truth and is fully auditable.
"""
import logging
import uuid

from sqlalchemy import func

from app.db import get_session
from app.models import CreditLedger

logger = logging.getLogger("socioburp.credits")


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


def charge_for_generation(business_id: uuid.UUID, generation_id: uuid.UUID, amount: int = 1):
    """
    Standalone charge — used by the engine right after marking a generation
    'done'. Kept as its own session/transaction, separate from add_credits(),
    for the common case where the caller doesn't already have a session open.
    """
    with get_session() as db:
        add_credits(db, business_id, -amount, reason="generation", ref_id=str(generation_id))
