"""OAuth: the network each registration and each request came from

Revision ID: f3c8a1d5e7b2
Revises: e5b2c7d9a1f3
Create Date: 2026-10-16 09:30:00

/register and /authorize answer anyone and each writes a row. They are
bounded (16 Oct 2026) in the service, not in Caddy: the rows of
one network in the last hour are counted, as POST /v1/agents/init counts
the accounts of one network in a day (hosted/signup.py). A nullable column
on each table, filled from now on; the rows of before have none and count
for nobody.
"""
import sqlalchemy as sa
from alembic import op

revision = "f3c8a1d5e7b2"
down_revision = "e5b2c7d9a1f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("oauth_clients", sa.Column("network", sa.String(64), nullable=True))
    op.add_column("oauth_grants", sa.Column("network", sa.String(64), nullable=True))
    op.create_index("idx_oauth_clients_network", "oauth_clients", ["network", "created_at"])
    op.create_index("idx_oauth_grants_network", "oauth_grants", ["network", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_oauth_grants_network", table_name="oauth_grants")
    op.drop_index("idx_oauth_clients_network", table_name="oauth_clients")
    op.drop_column("oauth_grants", "network")
    op.drop_column("oauth_clients", "network")
