"""an audit row says what a read returned

Revision ID: 8c3e5f2a7b19
Revises: 2f6c8e1a9d47
Create Date: 2026-10-01

The question a customer asks in a demo: "who wrote this memory, and what did
the agent know when it decided?". The trail recorded that a read happened and
not what it returned. `read_ids` holds the public ids of the memories and the
facts a read returned, at most 100 of each, with the counts in `meta`: with
`as_of` on /v1/facts they rebuild what the agent knew at that moment. Ids
only, never content, so an erasure stays true (services/audit.py).

Existing rows were written before anybody kept this and stay NULL. The
downgrade drops the column, and with it the ids recorded since.
"""
import sqlalchemy as sa
from alembic import op

revision = "8c3e5f2a7b19"
down_revision = "2f6c8e1a9d47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_events", sa.Column("read_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_events", "read_ids")
