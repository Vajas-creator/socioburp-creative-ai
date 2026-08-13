"""add owner_name to businesses

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "businesses",
        sa.Column("owner_name", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("businesses", "owner_name")
