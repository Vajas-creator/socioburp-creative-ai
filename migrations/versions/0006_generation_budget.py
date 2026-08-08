"""add regen_allowance_this_cycle and regens_used_this_cycle to businesses

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-03
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "businesses",
        sa.Column("regen_allowance_this_cycle", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "businesses",
        sa.Column("regens_used_this_cycle", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("businesses", "regens_used_this_cycle")
    op.drop_column("businesses", "regen_allowance_this_cycle")
