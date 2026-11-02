"""heartbeats: things that promise to ping, alerted on silence

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-02 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'heartbeats',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('kind', sa.String(length=10), server_default='job', nullable=False),
        sa.Column('interval_s', sa.Integer(), nullable=False),
        sa.Column('grace_s', sa.Integer(), nullable=False),
        sa.Column('last_ping_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_status', sa.String(length=10), server_default='ok', nullable=False),
        sa.Column('last_message', sa.String(length=300), nullable=True),
        sa.Column('missed_open', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('failed_open', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('good_streak', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('site_id', 'name', name='uq_heartbeat_site_name'),
    )


def downgrade() -> None:
    op.drop_table('heartbeats')
