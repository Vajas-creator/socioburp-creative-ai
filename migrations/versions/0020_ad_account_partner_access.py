"""add meta ad account / partner access columns to businesses, plus the
ad-account-connect pending-conversation column on conversation_state

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "businesses",
        sa.Column("meta_ad_account_id", sa.String(50), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column("meta_business_manager_id", sa.String(50), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "partner_access_status", sa.String(20), nullable=False,
            server_default="not_connected",
        ),
    )
    op.add_column(
        "conversation_state",
        sa.Column("pending_ad_account_connect", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("conversation_state", "pending_ad_account_connect")
    op.drop_column("businesses", "partner_access_status")
    op.drop_column("businesses", "meta_business_manager_id")
    op.drop_column("businesses", "meta_ad_account_id")
