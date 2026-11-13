"""memories can be found by their words too

Revision ID: 978ae0502f44
Revises: 8c3e5f2a7b19
Create Date: 2026-10-01 20:00:00
"""
from alembic import op

revision = "978ae0502f44"
down_revision = "8c3e5f2a7b19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """A full-text index on the memories' text, for the word side of the
    hybrid search (services/memory_engine.py, `_by_words`).

    An index on an EXPRESSION, not a stored tsvector column: adding a stored
    column rewrites the whole table, while this only builds an index next to
    it. The expression must be the one the search writes, character for
    character (`_TSV`), or Postgres will not use it. `simple`: every language
    indexed the same way, lowercase words with no stemming; the question's
    language only decides which of its function words to drop.

    Built at boot, before the server serves, like the vector index
    (4565f3e40de9). Memories are short texts, and a GIN build over them takes
    seconds per hundred thousand rows."""
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_mem_content_fts ON agent_memories "
        "USING gin (to_tsvector('simple'::regconfig, content))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_mem_content_fts")
