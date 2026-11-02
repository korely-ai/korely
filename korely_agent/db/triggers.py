"""What the database writes by itself: when the store learned a fact ended,
and which memory wrote it first.

`agent_facts.created_at` is when the store learned a fact (its transaction
time, the t'_created of Graphiti, Rasmussen et al. 2025, section 2.2.3).
`closed_known_at` is when it learned the fact had ended (Graphiti's
t'_expired), which `invalid_at` does not say: `invalid_at` is when the fact
stopped being true in the world, and a move made in September and told in
October closes the old address on 1 September, on 20 October
(`known_as_of`, 11 Oct 2026).

A trigger, not the code, because closures are written in many places, and
some of them belong to the shared engine (memory_core: the judge, the plan
that comes true, the memory edited), which this repository does not edit, or
are bulk UPDATEs that no ORM event sees (a forget of a whole end user). The
trigger sees every one of them, whoever writes it.

- A row inserted already closed (a fact told in the past tense, one closed by
  a newer statement already in the store) learned its end with its start:
  `now()`, the transaction's time, which is also its `created_at`.
- An `invalid_at` that changes (set, moved, cleared) is learned now, unless
  the same statement sets `closed_known_at` itself (a restore, a backfill):
  then that value stands.
- An `invalid_at` cleared (a closure undone) clears it too.

`now()` and not `clock_timestamp()`: the time of the transaction, the same
clock as the columns' defaults, so a fact closed by a statement written in
the same transaction reads as closed when the closer was learned.

Never before the fact's own `created_at`, though: a write's transaction lasts
as long as its model calls, and a fact another worker wrote and committed in
the meantime, then closed by this transaction, read as learned closed before
it was learned at all (the audit of 17 Oct 2026; `known_as_of` answered
"closed" for a moment the store showed it open). The migration that brought
the column backfilled with the same GREATEST.
"""
from __future__ import annotations

FUNCTION = """
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

TRIGGER = """
CREATE TRIGGER trg_agent_facts_closed_known_at
BEFORE INSERT OR UPDATE OF invalid_at ON agent_facts
FOR EACH ROW EXECUTE FUNCTION agent_facts_closed_known_at()
"""


# The first writer (the audit before 1.0.0): `source_memory_id` as the row is inserted,
# whoever inserts it (the engine's write, which this repository does not
# edit, a correction, a restore that carries its own value keeps it). Only on
# INSERT: nothing moves it afterwards, and the foreign key's ON DELETE SET
# NULL may still clear it when that memory is deleted.
FIRST_WRITER_FUNCTION = """
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

FIRST_WRITER_TRIGGER = """
CREATE TRIGGER trg_agent_facts_first_memory_id
BEFORE INSERT ON agent_facts
FOR EACH ROW EXECUTE FUNCTION agent_facts_first_memory_id()
"""


def install(table) -> None:
    """Create the functions and the triggers with the table (Base.metadata.
    create_all: the tests, a fresh install made without migrations). The
    migrations that brought the columns create them on an existing install."""
    from sqlalchemy import DDL, event

    event.listen(table, "after_create", DDL(FUNCTION).execute_if(dialect="postgresql"))
    event.listen(table, "after_create", DDL(TRIGGER).execute_if(dialect="postgresql"))
    for ddl in (FIRST_WRITER_FUNCTION, FIRST_WRITER_TRIGGER):
        event.listen(table, "after_create", DDL(ddl).execute_if(dialect="postgresql"))
