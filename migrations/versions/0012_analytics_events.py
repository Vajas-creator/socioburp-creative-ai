"""add analytics_events table

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "analytics_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id"), index=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("event_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_analytics_events_type", "analytics_events", ["event_type"])


def downgrade():
    op.drop_index("idx_analytics_events_type", table_name="analytics_events")
    op.drop_table("analytics_events")
