"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')  # for gen_random_uuid()

    op.create_table(
        "businesses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("phone", sa.String(20), nullable=False, unique=True),
        sa.Column("name", sa.String(200)),
        sa.Column("industry", sa.String(100)),
        sa.Column("onboarding_state", sa.String(50), server_default="new"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_businesses_phone", "businesses", ["phone"])

    op.create_table(
        "brand_profiles",
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id"), primary_key=True),
        sa.Column("logo_url", sa.Text),
        sa.Column("primary_color", sa.String(7)),
        sa.Column("secondary_color", sa.String(7)),
        sa.Column("tone", sa.String(50)),
        sa.Column("target_audience", sa.String(200)),
        sa.Column("website", sa.String(200)),
        sa.Column("contact_phone", sa.String(20)),
        sa.Column("address", sa.Text),
        sa.Column("extras", postgresql.JSONB, server_default="{}"),
    )

    op.create_table(
        "generations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id")),
        sa.Column("user_message", sa.Text, nullable=False),
        sa.Column("built_prompt", sa.Text),
        sa.Column("image_url", sa.Text),
        sa.Column("caption", sa.Text),
        sa.Column("hashtags", sa.Text),
        sa.Column("quality_score", sa.Integer),
        sa.Column("credits_charged", sa.Integer, server_default="1"),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("generations.id"), nullable=True),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_generations_business", "generations", ["business_id", "created_at"])

    op.create_table(
        "credit_ledger",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id")),
        sa.Column("delta", sa.Integer, nullable=False),
        sa.Column("reason", sa.String(50), nullable=False),
        sa.Column("ref_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_ledger_business", "credit_ledger", ["business_id"])

    op.create_table(
        "conversation_state",
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id"), primary_key=True),
        sa.Column("last_generation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("context", postgresql.JSONB, server_default="{}"),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("conversation_state")
    op.drop_table("credit_ledger")
    op.drop_table("generations")
    op.drop_table("brand_profiles")
    op.drop_table("businesses")
