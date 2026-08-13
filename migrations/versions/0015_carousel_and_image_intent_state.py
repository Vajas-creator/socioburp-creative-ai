"""add pending_carousel and pending_image_intent to conversation_state

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "conversation_state",
        sa.Column("pending_carousel", sa.Text(), nullable=True),
    )
    op.add_column(
        "conversation_state",
        sa.Column("pending_image_intent", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("conversation_state", "pending_image_intent")
    op.drop_column("conversation_state", "pending_carousel")
