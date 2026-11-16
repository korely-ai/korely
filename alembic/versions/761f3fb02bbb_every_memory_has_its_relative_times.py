"""every memory can carry its relative times, dated from its day

Revision ID: 761f3fb02bbb
Revises: b7d41c9e2a60
Create Date: 2026-10-06 01:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = "761f3fb02bbb"
down_revision = "b7d41c9e2a60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """A nullable JSON column: adding it rewrites nothing, so it is instant on
    any table size. memory_core 2.9.0 writes it with the facts when the
    relative-times rule is on (the expressions the model read, "yesterday",
    "two weekends ago", and the date or period the code computed from the
    memory's day, `relative_time.annotate`); /v1/context shows the date next
    to the words of a memory it shows whole. The hosted product's twin is its
    migration r57."""
    op.add_column("agent_memories", sa.Column("relative_times", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_memories", "relative_times")
