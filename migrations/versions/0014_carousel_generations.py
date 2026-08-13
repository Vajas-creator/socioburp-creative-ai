"""add carousel_image_urls to generations

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-12
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "generations",
        sa.Column("carousel_image_urls", postgresql.JSONB(), nullable=True),
    )


def downgrade():
    op.drop_column("generations", "carousel_image_urls")
