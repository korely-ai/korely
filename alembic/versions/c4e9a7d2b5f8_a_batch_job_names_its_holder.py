"""a /v1/batch job's claim names the process that holds it

Revision ID: c4e9a7d2b5f8
Revises: b7e3f1a9c2d4
Create Date: 2026-11-26 12:00:00

`agent_batch_jobs.claimed_by`, NULL by default: the key of the process
whose worker imports the job (korely_agent/services/holder.py), as
`agent_memories.graph_sync_claimed_by` (57a77fbaad61) is for a memory's
claim. A job left 'processing' with a key no session of the database holds
is a process's that has ended (a deploy, a restart, a crash), and goes back
to the queue at the next pass of any process instead of after
DRAIN_STALE_MINUTES (services/batch.reclaim_interrupted). Only the claim
sets it (services/batch.process_batch_jobs), each claim its own key, and it
is read only on a job still 'processing': no trigger is needed, as it is
for the memories, whose claim moves in many places.

Existing rows get no stamp: a job left 'processing' by the release before
this one is given back by the clock, as that release would have. Adding a
nullable column with no default changes no row (PostgreSQL 11 and later),
but it takes an ACCESS EXCLUSIVE lock on the table for that moment: each
attempt waits at most LOCK_TIMEOUT for it, inside a savepoint, ATTEMPTS in
all, with e5c9a2f7d3b1's helper, as b7e3f1a9c2d4 does, so a long
transaction on the table makes the start fail instead of queueing every
request behind the migration.

The downgrade drops the column; the release before reads the clock alone,
as it always did.
"""
import importlib.util
import os

import sqlalchemy as sa
from alembic import op

revision = "c4e9a7d2b5f8"
down_revision = "b7e3f1a9c2d4"
branch_labels = None
depends_on = None


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
    _with_the_lock(op.get_bind(), lambda: op.add_column(
        "agent_batch_jobs", sa.Column("claimed_by", sa.Integer(), nullable=True)))


def downgrade() -> None:
    op.drop_column("agent_batch_jobs", "claimed_by")
