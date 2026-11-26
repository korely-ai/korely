"""a fact's end is never learned before the fact

Revision ID: b8e4d2f6a9c1
Revises: e7c3a9b5d2f4
Create Date: 2026-10-17 23:00:00

The audit of 17 Oct 2026: the trigger that writes `closed_known_at` wrote
`now()`, the start of the transaction that closes the fact, and a write's
transaction lasts as long as its model calls. A fact another worker wrote and
committed meanwhile, then closed by that transaction, read as learned closed
before it was learned (`created_at`), and `known_as_of` answered "closed" for a
moment the store showed it open. The trigger now writes the later of the two,
as the backfill of e3a9c5d1f7b2 did (korely_agent/db/triggers.py has the same
SQL), and the rows written so far are put right.

The downgrade puts the old function back; the rows stay corrected.
"""
from alembic import op

revision = "b8e4d2f6a9c1"
down_revision = "e7c3a9b5d2f4"
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
            NEW.closed_known_at := GREATEST(now(), NEW.created_at);
        END IF;
    ELSIF NEW.invalid_at IS DISTINCT FROM OLD.invalid_at
          AND NEW.closed_known_at IS NOT DISTINCT FROM OLD.closed_known_at THEN
        NEW.closed_known_at := CASE WHEN NEW.invalid_at IS NULL THEN NULL
                                    ELSE GREATEST(now(), NEW.created_at) END;
    END IF;
    RETURN NEW;
END
$$
"""

_OLD_FUNCTION = """
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


def upgrade() -> None:
    op.execute(_FUNCTION)
    # Only `closed_known_at` changes, so the trigger (on UPDATE OF invalid_at)
    # does not fire.
    op.execute("""
        UPDATE agent_facts
           SET closed_known_at = created_at
         WHERE closed_known_at < created_at
    """)


def downgrade() -> None:
    op.execute(_OLD_FUNCTION)
