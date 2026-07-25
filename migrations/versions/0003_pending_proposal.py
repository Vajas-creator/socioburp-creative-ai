"""add pending_proposal to conversation_state

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-25
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversation_state", sa.Column("pending_proposal", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("conversation_state", "pending_proposal")
