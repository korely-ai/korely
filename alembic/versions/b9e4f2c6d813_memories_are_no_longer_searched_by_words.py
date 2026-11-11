"""memories are no longer searched by their words

Revision ID: b9e4f2c6d813
Revises: a5d81f3c7e64
Create Date: 2026-10-12 12:00:00

The word side of the memory search (HYBRID_SEARCH, 978ae0502f44) was off by
default since its first measure and measured worse (LongMemEval: evidence in
the top ten 83.1% against 85.6% for the cosine alone, five times slower); it
was removed on 12 Oct 2026. Its full-text index was kept up to
date by every write for a search nobody ran: it goes. The downgrade builds it
again, as 978ae0502f44 did.
"""
from alembic import op

revision = "b9e4f2c6d813"
down_revision = "a5d81f3c7e64"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_mem_content_fts")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_mem_content_fts ON agent_memories "
        "USING gin (to_tsvector('simple'::regconfig, content))"
    )
