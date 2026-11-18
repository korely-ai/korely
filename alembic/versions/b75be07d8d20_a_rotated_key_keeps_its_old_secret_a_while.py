"""a rotated key keeps its old secret for a while

Revision ID: b75be07d8d20
Revises: 57a77fbaad61
Create Date: 2026-11-14 12:00:00

`agent_api_keys.previous_key_hash` and `previous_expires_at`, NULL by
default: the hash of the secret a rotation replaced
(POST /admin/keys/{id}/rotate, services/keys.reissue_secret) and the moment
it stops working, so the agents that hold it have time to take the new one.
Unique, as `key_hash` is: one secret, one key. And `rotated_at`, when the
current secret was made if not at the key's creation: a rotation keeps the
lifetime the secret was given, counted from there. A key never rotated has
none of the three, as every existing row.

The downgrade drops the three columns: a secret still in its overlap stops
working at once, and the new one goes on.
"""
import sqlalchemy as sa
from alembic import op

revision = "b75be07d8d20"
down_revision = "57a77fbaad61"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_api_keys", sa.Column("previous_key_hash", sa.String(64), nullable=True))
    op.add_column("agent_api_keys", sa.Column("previous_expires_at", sa.DateTime(timezone=True),
                                              nullable=True))
    op.add_column("agent_api_keys", sa.Column("rotated_at", sa.DateTime(timezone=True),
                                              nullable=True))
    op.create_index("uq_agent_api_keys_previous_hash", "agent_api_keys", ["previous_key_hash"],
                    unique=True)


def downgrade() -> None:
    op.drop_index("uq_agent_api_keys_previous_hash", table_name="agent_api_keys")
    op.drop_column("agent_api_keys", "rotated_at")
    op.drop_column("agent_api_keys", "previous_expires_at")
    op.drop_column("agent_api_keys", "previous_key_hash")
