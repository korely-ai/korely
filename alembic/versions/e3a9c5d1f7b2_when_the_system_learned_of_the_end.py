"""when the store learned a fact had ended

Revision ID: e3a9c5d1f7b2
Revises: c2e8a4f6d913
Create Date: 2026-10-11 18:30:00

`known_as_of` (11 Oct 2026): the facts as the store knew
them at a date. `created_at` is when a fact was learned; this adds when its
end was learned, `closed_known_at`, which a trigger fills from now on whoever
closes the fact (korely_agent/db/triggers.py has why, and the same SQL).

The facts closed before this migration get it once, from what is stored, in
this order (an estimate, and the documentation says so):

  1. closed by another fact: when that fact was learned, never before the
     closed one itself (a fact learned after the newer one that closes it
     learned its end with its start);
  2. forgotten by itself (POST /v1/facts/{id}/forget): its first forget in
     agent_forget_audit, the only closure that can carry a date of its own;
  3. otherwise, the earlier of its end date and its last change, never before
     it was learned: a closure "now" (a forget of a whole end user, a memory
     edited) wrote its end date when it happened; a fact told in the past
     tense ended before it was learned, and learned its end with it.

The downgrade drops the trigger, its function and the column.
"""
import sqlalchemy as sa
from alembic import op

revision = "e3a9c5d1f7b2"
down_revision = "c2e8a4f6d913"
branch_labels = None
depends_on = None

_FUNCTION = """
CREATE OR REPLACE FUNCTION agent_facts_closed_known_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.invalid_at IS NULL THEN
            NEW.closed_known_at := NULL;
        ELSIF NEW.closed_known_at IS NULL THEN
            NEW.closed_known_at := now();
        END IF;
    ELSIF NEW.invalid_at IS DISTINCT FROM OLD.invalid_at
          AND NEW.closed_known_at IS NOT DISTINCT FROM OLD.closed_known_at THEN
        NEW.closed_known_at := CASE WHEN NEW.invalid_at IS NULL THEN NULL ELSE now() END;
    END IF;
    RETURN NEW;
END
$$
"""

_TRIGGER = """
CREATE TRIGGER trg_agent_facts_closed_known_at
BEFORE INSERT OR UPDATE OF invalid_at ON agent_facts
FOR EACH ROW EXECUTE FUNCTION agent_facts_closed_known_at()
"""


def upgrade() -> None:
    op.add_column("agent_facts", sa.Column("closed_known_at", sa.DateTime(timezone=True),
                                           nullable=True))
    # 1. Closed by another fact.
    op.execute("""
        UPDATE agent_facts AS f
           SET closed_known_at = GREATEST(f.created_at, c.created_at)
          FROM agent_facts AS c
         WHERE f.invalidated_by_id = c.id
           AND f.invalid_at IS NOT NULL
           AND f.closed_known_at IS NULL
    """)
    # 2. Forgotten by itself: the first forget that closed it.
    op.execute("""
        UPDATE agent_facts AS f
           SET closed_known_at = GREATEST(f.created_at, a.first_forget)
          FROM (SELECT tenant_id, target_id, min(created_at) AS first_forget
                  FROM agent_forget_audit
                 WHERE scope = 'fact' AND facts_invalidated > 0
                 GROUP BY tenant_id, target_id) AS a
         WHERE a.tenant_id = f.tenant_id
           AND a.target_id = 'fct_' || replace(f.id::text, '-', '')
           AND f.invalid_at IS NOT NULL
           AND f.closed_known_at IS NULL
    """)
    # 3. The rest.
    op.execute("""
        UPDATE agent_facts
           SET closed_known_at = GREATEST(created_at, LEAST(invalid_at, updated_at))
         WHERE invalid_at IS NOT NULL
           AND closed_known_at IS NULL
    """)
    # From now on, the database fills it.
    op.execute(_FUNCTION)
    op.execute(_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_agent_facts_closed_known_at ON agent_facts")
    op.execute("DROP FUNCTION IF EXISTS agent_facts_closed_known_at()")
    op.drop_column("agent_facts", "closed_known_at")
