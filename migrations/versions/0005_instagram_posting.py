"""add instagram_account_id to businesses, posted_to_instagram to generations

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-03
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "businesses",
        sa.Column("instagram_account_id", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "generations",
        sa.Column("posted_to_instagram", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("generations", "posted_to_instagram")
    op.drop_column("businesses", "instagram_account_id")
