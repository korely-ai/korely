"""every memory can carry an observation about its end user

Revision ID: b7d41c9e2a60
Revises: 978ae0502f44
Create Date: 2026-10-02 01:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = "b7d41c9e2a60"
down_revision = "978ae0502f44"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """A nullable text column: adding it rewrites nothing (Postgres stores a
    missing value for the old rows), so it is instant on any table size.
    Written by the extraction from memory_core 2.6 on, read by /v1/context
    in place of the memory's whole text."""
    op.add_column("agent_memories", sa.Column("observation", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_memories", "observation")
