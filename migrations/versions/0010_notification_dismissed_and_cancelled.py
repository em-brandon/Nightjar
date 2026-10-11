"""notification dismissed and cancelled

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-11 11:40:39.278581
"""
from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('notifications', sa.Column('dismissed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('notifications', sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('notifications', 'cancelled_at')
    op.drop_column('notifications', 'dismissed_at')
