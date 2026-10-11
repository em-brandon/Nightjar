"""daily stats rollup

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-11 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'daily_stats',
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('checks_total', sa.Integer(), server_default='0', nullable=False),
        sa.Column('checks_ok', sa.Integer(), server_default='0', nullable=False),
        sa.Column('latency_avg_ms', sa.Integer(), nullable=True),
        sa.Column('latency_p95_ms', sa.Integer(), nullable=True),
        sa.Column('events', sa.Integer(), server_default='0', nullable=False),
        sa.Column('issues_opened', sa.Integer(), server_default='0', nullable=False),
        sa.Column('alerts_sent', sa.Integer(), server_default='0', nullable=False),
        sa.Column('alerts_failed', sa.Integer(), server_default='0', nullable=False),
        sa.Column('p1_expired', sa.Integer(), server_default='0', nullable=False),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('site_id', 'day'),
    )


def downgrade() -> None:
    op.drop_table('daily_stats')
