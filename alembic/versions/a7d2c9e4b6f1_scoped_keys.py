"""Keys confined to some end users of their project

Revision ID: a7d2c9e4b6f1
Revises: f3c8a1d5e7b2
Create Date: 2026-10-16 19:00:00

Since 16 Oct 2026 (services/user_scope.py) a key, or an
OAuth grant at consent, may list the end users it reads and writes, exact
ids and prefixes ending in `*`. A nullable column on each table: NULL is the
whole project, which is every key of before.
"""
import sqlalchemy as sa
from alembic import op

revision = "a7d2c9e4b6f1"
down_revision = "f3c8a1d5e7b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_api_keys", sa.Column("end_users", sa.JSON(), nullable=True))
    op.add_column("oauth_grants", sa.Column("end_users", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("oauth_grants", "end_users")
    op.drop_column("agent_api_keys", "end_users")
