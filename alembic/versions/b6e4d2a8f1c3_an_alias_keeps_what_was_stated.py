"""an entity alias keeps the canonical it was stated with

Revision ID: b6e4d2a8f1c3
Revises: c5f9d3b7e1a2
Create Date: 2026-10-20 12:00:00

The audit of 20 Oct 2026: when a rename is learned ("Acme -> Globex"), every
alias of the memory pointing at "Acme" is rewritten to point at "Globex" (the
chain flattened, memory_core `store.add_alias`), with a bulk UPDATE that
recorded neither which rows it changed nor what they pointed at before.
Erasing the memory that taught the rename then had to guess which rows to
point back, and pointed back a hand-declared "GX -> Globex" the rename never
touched.

`stated_canonical` is the canonical as the alias was written, never rewritten
by a flattening: undoing an alias recomputes the chains from what was stated
and changes only the rows whose chain went through it.

Every existing row takes its canonical as it is now: which rows a flattening
rewrote before this revision is not recorded anywhere, so an undo leaves them
as they are rather than guessing. The downgrade drops the column.
"""
import sqlalchemy as sa
from alembic import op

revision = "b6e4d2a8f1c3"
down_revision = "c5f9d3b7e1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_entity_aliases",
                  sa.Column("stated_canonical", sa.String(length=200), nullable=True))
    op.execute("UPDATE agent_entity_aliases SET stated_canonical = canonical_label")


def downgrade() -> None:
    op.drop_column("agent_entity_aliases", "stated_canonical")
