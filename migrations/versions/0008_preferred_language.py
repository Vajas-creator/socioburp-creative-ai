"""add preferred_language to businesses

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "businesses",
        sa.Column("preferred_language", sa.String(length=10), nullable=True),
    )


def downgrade():
    op.drop_column("businesses", "preferred_language")
