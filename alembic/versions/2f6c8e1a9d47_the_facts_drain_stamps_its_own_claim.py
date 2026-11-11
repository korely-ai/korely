"""the facts drain stamps its own claim, and updated_at is the developer's again

Revision ID: 2f6c8e1a9d47
Revises: 7d4e2b9a1c63
Create Date: 2026-09-28

A memory's `updated_at` is what the API returns, what the history of a memory
reads its "updated" event off, and what PATCH checks `expected_updated_at`
against. The model declared it with the ORM's automatic `onupdate`, so every
write to the row moved it, the worker's bookkeeping included: the facts
drain's claim ('processing') and its outcome, the reaper, the vectors,
`manage reextract`, the move of a deleted project's rows. Every memory
processed in the background read as edited by nobody, and a caller holding
the version it had been given got 409 `stale_write` once the drain had passed.

The `onupdate` is gone from the model and the one path that edits sets the
column (`AgentMemoryEngine.update`). That is Python-side and changes no
schema. What does: the reaper took back a claim older than
DRAIN_STALE_MINUTES by reading `updated_at`, which it could only do because
the claim moved it. The claim has a column of its own now,
`graph_sync_claimed_at`.

Existing rows. A memory left 'processing' by the previous version gets its
claim time from where that version wrote it (`updated_at`, else
`created_at`), so the upgrade neither steals a claim that is still live nor
strands one; every other row has no claim, NULL. The `updated_at` values the
old bookkeeping wrote are left as they are: nothing records which of them
were edits. The downgrade drops the column; the previous version reads the
claim off `updated_at` again, which it moves itself.
"""
import sqlalchemy as sa
from alembic import op

revision = "2f6c8e1a9d47"
down_revision = "7d4e2b9a1c63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_memories", sa.Column("graph_sync_claimed_at",
                                              sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE agent_memories SET graph_sync_claimed_at = coalesce(updated_at, created_at)"
        " WHERE graph_sync_status = 'processing'"
    )


def downgrade() -> None:
    op.drop_column("agent_memories", "graph_sync_claimed_at")
