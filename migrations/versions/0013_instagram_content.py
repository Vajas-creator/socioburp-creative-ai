"""add instagram_bio and instagram_recent_captions to brand_profiles

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-12
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "brand_profiles",
        sa.Column("instagram_bio", sa.Text(), nullable=True),
    )
    op.add_column(
        "brand_profiles",
        sa.Column("instagram_recent_captions", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("brand_profiles", "instagram_recent_captions")
    op.drop_column("brand_profiles", "instagram_bio")
