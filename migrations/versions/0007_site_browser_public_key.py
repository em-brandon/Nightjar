"""site browser public key and allowed origins

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-09 20:24:42.514820
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('sites', sa.Column('public_key', sa.String(length=64), nullable=True))
    op.add_column('sites', sa.Column('allowed_origins', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False))
    # Named as Postgres names an inline UNIQUE, so `alembic check` sees the model and the database agree
    op.create_unique_constraint('sites_public_key_key', 'sites', ['public_key'])


def downgrade() -> None:
    op.drop_constraint('sites_public_key_key', 'sites', type_='unique')
    op.drop_column('sites', 'allowed_origins')
    op.drop_column('sites', 'public_key')
