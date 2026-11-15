"""an account keeps the ids of the accounts moved into it

Revision ID: f2c8a4d6e1b3
Revises: d4b7e2a9c8f1
Create Date: 2026-10-19 12:00:00

The audit of 19 Oct 2026: on the Cloud, a key's account claimed into
an account that already holds data moves every row into it
(korely_agent/hosted/claim.py). The pseudonyms its erasures left
(korely_agent/services/pseudonyms.py) are an HMAC of `account:end user`,
keyed to the account they were made in, and cannot be keyed again without
the end user's id: in the receiving account GET /v1/audit?user_id= no longer
found the proof of the erasure, and a later trail row about that id was
written in clear. `tenants.merged_tenant_ids` keeps the ids of the accounts
moved in, and the lookups by id try them too.

Nothing to fill: a move made before this is not recorded anywhere (the
moved account's rows went, its tenant row with them).

The downgrade drops the column and the ids it held.
"""
import sqlalchemy as sa
from alembic import op

revision = "f2c8a4d6e1b3"
down_revision = "d4b7e2a9c8f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("merged_tenant_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "merged_tenant_ids")
