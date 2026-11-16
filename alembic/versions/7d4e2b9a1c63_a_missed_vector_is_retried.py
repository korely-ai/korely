"""a vector that did not come back is retried, a bounded number of times

Revision ID: 7d4e2b9a1c63
Revises: b64f02d8e9a1
Create Date: 2026-09-28

A memory or a fact is found by meaning only through its vector: memory search
and the ranking of facts in /v1/context read the rows that have one, and so
does the semantic half of the contradiction check. Until now a vector whose
call failed was never computed again. A memory stayed out of similarity search
for good (the code said "a future worker can backfill", and there was none);
an update whose call failed kept the OLD vector, marked done, on the new text;
and a fact write with a key set was refused outright (the round of the same
morning), where the engine's earlier home kept the fact and retried its
vector.

The row is the job (services/vectors.py): `embedding_status` 'pending' with no
vector is waiting for one, `embedding_attempts` counts the calls made for the
current text, `embedding_next_at` is when the drain may make the next one
(NULL: at its next pass). After the last attempt the row is 'error'.

Existing rows. Nothing is rewritten: every row starts at zero attempts, due at
once. Those already 'pending' with no vector (written with no key, or whose
best-effort call failed) are therefore taken by the drain as soon as an
embedder is configured, a few per pass (VECTOR_DRAIN_LIMIT): the backfill that
was promised and never ran. An install with no Google key has no drain to run
and keeps them as they are.

The two indexes are partial, on the rows still waiting, which is all the drain
reads. The downgrade drops the indexes and the columns; the vectors written
meanwhile stay, since the previous version reads them the same way.
"""
import sqlalchemy as sa
from alembic import op

revision = "7d4e2b9a1c63"
down_revision = "b64f02d8e9a1"
branch_labels = None
depends_on = None

_TABLES = (
    ("agent_memories", "idx_agent_mem_vector_waiting"),
    ("agent_facts", "idx_agent_facts_vector_waiting"),
)


def upgrade() -> None:
    for table, index in _TABLES:
        op.add_column(table, sa.Column("embedding_attempts", sa.Integer(),
                                       nullable=False, server_default="0"))
        op.add_column(table, sa.Column("embedding_next_at",
                                       sa.DateTime(timezone=True), nullable=True))
        op.create_index(index, table, ["embedding_next_at"],
                        postgresql_where=sa.text("embedding_status = 'pending'"))


def downgrade() -> None:
    for table, index in _TABLES:
        op.drop_index(index, table_name=table)
        op.drop_column(table, "embedding_next_at")
        op.drop_column(table, "embedding_attempts")
