"""notification expired at

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-11 10:50:09.717431
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('notifications', sa.Column('expired_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('notifications', 'expired_at')
