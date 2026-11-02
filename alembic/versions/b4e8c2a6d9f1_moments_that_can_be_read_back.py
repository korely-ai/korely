"""the moments a caller stored outside what can be read back are mended

Revision ID: b4e8c2a6d9f1
Revises: d8b4e1a7c3f5
Create Date: 2026-10-20 09:00:00

The audit of 20 Oct 2026: a memory `timestamp` or a fact `valid_from`
of 0001-01-01 with a positive UTC offset ("0001-01-01T00:00:00+14:00", or
.NET's DateTime.MinValue written in a European zone) was accepted and stored
as a moment of the year 0 in UTC, which Postgres keeps and the driver cannot
read back (Python's years are 1 to 9999). The write answered 500 after
committing the row; the facts drain, which loads the earliest event first,
raised on it at every tick, so no memory of any account was mined again; and
every read of that end user's memories or facts answered 500.

The doors refuse such a moment since this revision (services/limits.moment_problem).
The rows already stored are brought here to the nearest moment that can be
read back, the bounds of services/limits (EARLIEST_MOMENT, LATEST_MOMENT:
one day inside Python's years, so no session time zone shows them outside):
the memories' event time and the facts' valid_from and invalid_at. A fact's
`closed_known_at` is the trigger's to write when `invalid_at` changes; the
trigger is held for that one statement, since the store did not learn of the
end now, only the date's spelling changed.

The downgrade leaves the rows as they are: the values of before could not be
read.
"""
from alembic import op

revision = "b4e8c2a6d9f1"
down_revision = "d8b4e1a7c3f5"
branch_labels = None
depends_on = None

# services/limits.EARLIEST_MOMENT and LATEST_MOMENT, as they were written
# with this revision (a migration does not import the code it migrates).
_EARLIEST = "TIMESTAMPTZ '0001-01-02 00:00:00+00'"
_LATEST = "TIMESTAMPTZ '9999-12-30 23:59:59.999999+00'"


def _mend(table: str, column: str) -> str:
    return (f"UPDATE {table} SET {column} = CASE WHEN {column} < {_EARLIEST} THEN {_EARLIEST} "
            f"ELSE {_LATEST} END WHERE {column} < {_EARLIEST} OR {column} > {_LATEST}")


def upgrade() -> None:
    op.execute(_mend("agent_memories", "event_time"))
    op.execute(_mend("agent_facts", "valid_from"))
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM agent_facts
                       WHERE invalid_at < {_EARLIEST} OR invalid_at > {_LATEST}) THEN
                ALTER TABLE agent_facts DISABLE TRIGGER trg_agent_facts_closed_known_at;
                {_mend("agent_facts", "invalid_at")};
                ALTER TABLE agent_facts ENABLE TRIGGER trg_agent_facts_closed_known_at;
            END IF;
        END
        $$
    """)


def downgrade() -> None:
    pass
