"""add instagram_handle to businesses

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "businesses",
        sa.Column("instagram_handle", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("businesses", "instagram_handle")
