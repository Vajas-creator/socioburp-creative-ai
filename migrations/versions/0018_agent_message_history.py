"""add agent_message_history to conversation_state

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "conversation_state",
        sa.Column("agent_message_history", postgresql.JSONB(), nullable=True),
    )


def downgrade():
    op.drop_column("conversation_state", "agent_message_history")
