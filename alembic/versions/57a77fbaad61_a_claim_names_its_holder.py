"""a memory's claim names the process that holds it

Revision ID: 57a77fbaad61
Revises: b6e2d9f4a8c1
Create Date: 2026-11-13 12:00:00

`agent_memories.graph_sync_claimed_by`, NULL by default: the key of the
process whose facts drain holds the claim (korely_agent/services/holder.py).
A claim whose key no session of the database holds is a process's that has
ended (a deploy, a restart, a crash), and goes back to the queue at the next
drain of any process instead of after DRAIN_STALE_MINUTES. A trigger clears
the stamp whenever the claim changes and the same UPDATE does not set it
(korely_agent/db/triggers.py has why, and the same SQL), so a stamp is
always the current claim's.

Existing rows get no stamp: a memory left 'processing' by the release before
this one is given back by the clock, as that release would have.

The downgrade drops the trigger, its function and the column; the release
before reads the claim time alone, as it always did.
"""
import sqlalchemy as sa
from alembic import op

revision = "57a77fbaad61"
down_revision = "b6e2d9f4a8c1"
branch_labels = None
depends_on = None

_FUNCTION = """
CREATE OR REPLACE FUNCTION agent_memories_claimed_by() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.graph_sync_status IS DISTINCT FROM OLD.graph_sync_status
        OR NEW.graph_sync_claimed_at IS DISTINCT FROM OLD.graph_sync_claimed_at)
       AND NEW.graph_sync_claimed_by IS NOT DISTINCT FROM OLD.graph_sync_claimed_by THEN
        NEW.graph_sync_claimed_by := NULL;
    END IF;
    RETURN NEW;
END
$$
"""

_TRIGGER = """
CREATE TRIGGER trg_agent_memories_claimed_by
BEFORE UPDATE OF graph_sync_status, graph_sync_claimed_at ON agent_memories
FOR EACH ROW EXECUTE FUNCTION agent_memories_claimed_by()
"""


def upgrade() -> None:
    op.add_column("agent_memories", sa.Column("graph_sync_claimed_by", sa.Integer(),
                                              nullable=True))
    op.execute(_FUNCTION)
    op.execute(_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_agent_memories_claimed_by ON agent_memories")
    op.execute("DROP FUNCTION IF EXISTS agent_memories_claimed_by()")
    op.drop_column("agent_memories", "graph_sync_claimed_by")
