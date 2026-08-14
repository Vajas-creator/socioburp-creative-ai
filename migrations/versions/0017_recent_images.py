"""add recent_images to conversation_state

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-14
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "conversation_state",
        sa.Column("recent_images", postgresql.JSONB(), nullable=True),
    )


def downgrade():
    op.drop_column("conversation_state", "recent_images")
