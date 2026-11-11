"""the memories that say a fact is over

Revision ID: c5e8b3f1a9d4
Revises: a7c4e9f2d1b6
Create Date: 2026-11-09 09:00:00

A memory that ends a fact in place, a past statement of it ("I no longer
live in Paris") or an edit of its text that no longer states it, writes on
the row only its end, and leaves the pointer an earlier judge gave the row
(`invalidated_by_id`). A deletion of the fact that pointer names read it as
the closing and gave the fact back while that memory still said it was over
(code review finding, P1 of 8 Nov 2026, and the skeptic's third round of 9
Nov). `agent_fact_ended_by` holds, for each fact, every memory whose own
words say it is over, written by that memory's write (facts/service.py,
facts/previous_version.py) and read by the deletion (facts/given_back.py).

Changed in place on 10 Nov 2026, before any release: it added one column,
`agent_facts.ended_by_memory_id`, which held only the first memory, so a
second memory that said the fact was over again did not keep it closed once
the first went (the skeptic's fourth round of 9 Nov). A table holds them
all, and a new empty table takes no lock that stops the reads of the facts
(the column's ADD COLUMN held the facts table, reads included, to the
commit).

The rows already stored get none: who ended them in place was never
written, and is not inferred from their clocks (the CHANGELOG and the
limits say what a deletion reads for them).

ON DELETE CASCADE on the three keys: a row goes with its fact, its memory
(an erasure) or its account. The primary key serves "the memories of this
fact"; the index on `memory_id` serves the cascade at each memory deleted
and "the facts this memory ended", which a deletion of the memory reads.

The downgrade drops the table.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c5e8b3f1a9d4"
down_revision = "a7c4e9f2d1b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_fact_ended_by",
        sa.Column("fact_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_facts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("memory_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_memories.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    )
    op.create_index("ix_agent_fact_ended_by_memory_id", "agent_fact_ended_by", ["memory_id"])
    op.create_index("ix_agent_fact_ended_by_tenant_id", "agent_fact_ended_by", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("agent_fact_ended_by")
