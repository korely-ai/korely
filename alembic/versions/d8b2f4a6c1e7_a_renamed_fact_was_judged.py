"""a fact whose names changed was judged when it was written

Revision ID: d8b2f4a6c1e7
Revises: b6e4d2a8f1c3
Create Date: 2026-10-27 12:00:00

The audit of 27 Oct 2026: declaring or removing a name clears the vector of
every fact whose canonical names change, and the vector drain computed it
again and then ran the late judgement meant for a fact whose vector failed on
its write: one more judge call per renamed fact, repeatable at the request
rate of a key.

`judged_before_vector` says the fact was judged when it was written and its
vector is being computed again only because its text changed: the drain
stores the vector and asks no judge, and puts the column back to false.
Every existing row is false, so a rename-cleared fact already waiting is
judged once more, as it was before. The downgrade drops the column.
"""
import sqlalchemy as sa
from alembic import op

revision = "d8b2f4a6c1e7"
down_revision = "b6e4d2a8f1c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_facts", sa.Column("judged_before_vector", sa.Boolean(), nullable=False,
                                           server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("agent_facts", "judged_before_vector")
