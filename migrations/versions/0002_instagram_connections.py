"""instagram connections

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instagram_connections",
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id"), primary_key=True),
        sa.Column("ig_user_id", sa.String(50), nullable=False),
        sa.Column("ig_username", sa.String(200)),
        sa.Column("page_id", sa.String(50), nullable=False),
        sa.Column("access_token", sa.Text, nullable=False),
        sa.Column("scopes", sa.Text),
        sa.Column("connected_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("instagram_connections")
