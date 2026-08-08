"""add pending_first_request to businesses

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "businesses",
        sa.Column("pending_first_request", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("businesses", "pending_first_request")
