"""alert outbox notifications

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-11 09:59:05.898255
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('notifications',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('issue_id', sa.Integer(), nullable=False),
    sa.Column('change', sa.String(length=20), nullable=False),
    sa.Column('changed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('severity', sa.String(length=4), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('speaker', sa.String(length=10), nullable=True),
    sa.Column('site', sa.String(length=120), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('status', sa.String(length=10), server_default='pending', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.String(length=300), nullable=True),
    sa.Column('receipt', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('acked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['issue_id'], ['issues.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('issue_id', 'change', 'changed_at', name='uq_notification_change')
    )
    op.create_index('ix_notifications_due', 'notifications', ['status', 'next_attempt_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_notifications_due', table_name='notifications')
    op.drop_table('notifications')
