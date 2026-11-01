"""site secret version, for secrets derived from a master key

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-01 11:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0 = the stored ingest_secret is the secret. Every existing site gets it, so nothing stops verifying on the day.
    op.add_column('sites', sa.Column('secret_version', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('sites', 'secret_version')
