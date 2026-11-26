"""the purge of revoked keys finds by index what points at a key

Revision ID: b7e3f1a9c2d4
Revises: a9d4c7e2f1b6
Create Date: 2026-11-25 17:00:00

1.0.40 deletes the API keys revoked longer ago than the trail is kept
(services/keys.purge_revoked). Each key deleted sets NULL the rows that
point at it, `audit_events.api_key_id` and `agent_batch_jobs.api_key_id`,
and neither column had an index: each key read the whole trail (300 keys
over a trail of 1,000,000 rows took 10.4 s, the review of 25 Nov 2026).
Two indexes, `ix_audit_events_api_key_id` and
`ix_agent_batch_jobs_api_key_id`.

Not CONCURRENTLY: alembic/env.py runs every pending migration in one
transaction, under the advisory lock that keeps two starting processes from
applying the same migration, and CREATE INDEX CONCURRENTLY cannot run inside
a transaction (an autocommit block would end that lock). A plain CREATE
INDEX takes a SHARE lock on its table: writes to it wait while the index is
built (reads go on). Each attempt waits at most LOCK_TIMEOUT for the lock,
inside a savepoint, ATTEMPTS in all, as e5c9a2f7d3b1 (whose docstring says
why and how), with its helper. The build's own time is the table's size:
not measured on the Cloud's trail.

The downgrade drops the two indexes; nothing else changes.
"""
import importlib.util
import os

from alembic import op

revision = "b7e3f1a9c2d4"
down_revision = "a9d4c7e2f1b6"
branch_labels = None
depends_on = None

_INDEXES = (("ix_audit_events_api_key_id", "audit_events"),
            ("ix_agent_batch_jobs_api_key_id", "agent_batch_jobs"))


def _with_the_lock(conn, do) -> None:
    """e5c9a2f7d3b1's `with_the_lock`, the one helper of the bounded lock
    waits, loaded from its file (a migration's module is not importable by
    name)."""
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "e5c9a2f7d3b1", os.path.join(here, "e5c9a2f7d3b1_a_long_memory_has_a_vector_for_each_window.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.with_the_lock(conn, do)


def upgrade() -> None:
    for name, table in _INDEXES:
        _with_the_lock(op.get_bind(), lambda n=name, t=table: op.create_index(n, t, ["api_key_id"]))


def downgrade() -> None:
    for name, table in _INDEXES:
        op.drop_index(name, table_name=table)
