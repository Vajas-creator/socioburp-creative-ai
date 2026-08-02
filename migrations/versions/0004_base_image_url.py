"""add base_image_url to generations (pre-composite background, for free logo-move revisions)

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("generations", sa.Column("base_image_url", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("generations", "base_image_url")
