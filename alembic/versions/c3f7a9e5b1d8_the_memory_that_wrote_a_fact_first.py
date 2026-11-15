"""the memory that wrote a fact first

Revision ID: c3f7a9e5b1d8
Revises: e7c4b2d9a6f1
Create Date: 2026-10-28 10:00:00

The audit before 1.0.0 and its review. A forget or an edit re-points
`source_memory_id` of a fact another memory still states to that memory, so
the erasure of the memory that wrote it first, under the new name of a rename
it taught, no longer found it, and inferring it from the labels deleted facts
the erased memory never wrote. `first_memory_id` keeps the link:
`source_memory_id` as the row is inserted, set by a trigger
(korely_agent/db/triggers.py has the same SQL) and never moved; NULL once that
memory is deleted.

The facts already stored take their `source_memory_id` as it is now: the
first writer, except where a forget or an edit made before this version
re-pointed it already, which nothing recorded (the CHANGELOG says so). One
UPDATE: alembic/env.py runs the migrations in one transaction, and ADD
COLUMN holds the facts table's ACCESS EXCLUSIVE lock until it commits, so
the facts table is locked for the whole migration, about 25 s per million
facts (the review's measure at 400,000). The API must not serve meanwhile;
the entrypoint migrates before it starts.

The downgrade drops the trigger, its function and the column.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c3f7a9e5b1d8"
down_revision = "e7c4b2d9a6f1"
branch_labels = None
depends_on = None

_FUNCTION = """
CREATE OR REPLACE FUNCTION agent_facts_first_memory_id() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.first_memory_id IS NULL THEN
        NEW.first_memory_id := NEW.source_memory_id;
    END IF;
    RETURN NEW;
END
$$
"""

_TRIGGER = """
CREATE TRIGGER trg_agent_facts_first_memory_id
BEFORE INSERT ON agent_facts
FOR EACH ROW EXECUTE FUNCTION agent_facts_first_memory_id()
"""

def upgrade() -> None:
    op.add_column("agent_facts", sa.Column("first_memory_id", postgresql.UUID(as_uuid=True),
                                           nullable=True))
    op.execute(_FUNCTION)
    op.execute(_TRIGGER)
    op.execute("UPDATE agent_facts SET first_memory_id = source_memory_id"
               " WHERE source_memory_id IS NOT NULL")
    op.create_foreign_key(None, "agent_facts", "agent_memories", ["first_memory_id"], ["id"],
                          ondelete="SET NULL")
    op.create_index(op.f("ix_agent_facts_first_memory_id"), "agent_facts", ["first_memory_id"],
                    unique=False)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_agent_facts_first_memory_id ON agent_facts")
    op.execute("DROP FUNCTION IF EXISTS agent_facts_first_memory_id()")
    op.drop_index(op.f("ix_agent_facts_first_memory_id"), table_name="agent_facts")
    op.drop_column("agent_facts", "first_memory_id")
