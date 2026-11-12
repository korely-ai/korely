"""the forget records are read by the fact they name

Revision ID: a7c4e9f2d1b6
Revises: d8a4f2c6b1e9
Create Date: 2026-11-09 06:00:00

A deletion now reads the records of the facts it would give back: a fact
forgotten or corrected by hand is never given back (facts/given_back.py,
`_said_by_hand`). The table had indexes on the account, the project and the
end user only, so the read went through every record of the account: 20 ms a
deletion with 200,000 records, read in full (measured by the skeptic of the
piece, 9 Nov 2026). The index serves "these facts of this account".

The downgrade drops the index; nothing else changes.
"""
from alembic import op

revision = "a7c4e9f2d1b6"
down_revision = "d8a4f2c6b1e9"
branch_labels = None
depends_on = None

INDEX = "idx_agent_forget_audit_tenant_target"


def upgrade() -> None:
    op.create_index(INDEX, "agent_forget_audit", ["tenant_id", "target_id"])


def downgrade() -> None:
    op.drop_index(INDEX, table_name="agent_forget_audit")
