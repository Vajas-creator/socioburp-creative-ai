"""alter credit_ledger.ref_id to string (for Razorpay payment_link ids)

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "credit_ledger",
        "ref_id",
        existing_type=postgresql.UUID(as_uuid=True),
        type_=sa.String(64),
        postgresql_using="ref_id::text",
    )


def downgrade():
    op.alter_column(
        "credit_ledger",
        "ref_id",
        existing_type=sa.String(64),
        type_=postgresql.UUID(as_uuid=True),
        postgresql_using="ref_id::uuid",
    )
