"""add instagram insights oauth columns to businesses

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "businesses",
        sa.Column("instagram_insights_ig_user_id", sa.String(50), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column("instagram_insights_page_id", sa.String(50), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column("instagram_insights_access_token", sa.Text(), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column("instagram_insights_token_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column("instagram_insights_connected_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("businesses", "instagram_insights_connected_at")
    op.drop_column("businesses", "instagram_insights_token_expires_at")
    op.drop_column("businesses", "instagram_insights_access_token")
    op.drop_column("businesses", "instagram_insights_page_id")
    op.drop_column("businesses", "instagram_insights_ig_user_id")
