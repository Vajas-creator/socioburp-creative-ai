"""add generations.trigger_source and learning_events audit table

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "generations",
        sa.Column("trigger_source", sa.String(length=30), nullable=True),
    )
    op.create_table(
        "learning_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id"), index=True),
        sa.Column("generation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("generations.id"), nullable=True),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("quality_score", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_learning_events_business_id", "learning_events", ["business_id"])


def downgrade():
    op.drop_index("ix_learning_events_business_id", table_name="learning_events")
    op.drop_table("learning_events")
    op.drop_column("generations", "trigger_source")
