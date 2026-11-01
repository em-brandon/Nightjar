"""audit log for sites and secrets

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-01 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'audit_log',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=True),
        sa.Column('site_name', sa.String(length=120), nullable=False),
        sa.Column('action', sa.String(length=40), nullable=False),
        sa.Column('detail', sa.String(length=300), nullable=False),
        sa.Column('actor', sa.String(length=40), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_audit_site_time', 'audit_log', ['site_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_audit_site_time', table_name='audit_log')
    op.drop_table('audit_log')
