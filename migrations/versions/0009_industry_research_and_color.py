"""add industry_style_research table

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "industry_style_research",
        sa.Column("industry", sa.String(length=100), primary_key=True),
        sa.Column("style_summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("industry_style_research")
