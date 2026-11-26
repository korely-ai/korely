"""a long memory has a vector for each window of its text

Revision ID: e5c9a2f7d3b1
Revises: d8a4f2c6e1b9
Create Date: 2026-11-17 11:00:00

`agent_memory_windows`: one row per window of a long memory's text, by
where it begins and ends in the memory's text (no copy of the text), with
its vector and the vector's job (the states and the ladder of the memories'
own vector, services/vector_state.py). Rows are written only with
MEMORY_WINDOWS on (services/memory_windows.py), at the write and by `manage
memory-windows`; with the setting off, as on every install by default, the
table stays empty and no search reads it. The rows go with the memory's
row (ON DELETE CASCADE) and with its account's.

Nothing is filled here: the windows of the memories already stored are
made by `manage memory-windows --apply`, in batches, when somebody turns
the setting on.

The downgrade drops the table: what it held is derived from the memories'
texts and is made again by the same command.

The foreign key to `agent_memories` takes a SHARE ROW EXCLUSIVE lock on that
table until the migration commits (read in pg_locks on 17 Nov 2026), which
waits for every write in flight and makes every write after it wait behind
it. So each attempt waits at most LOCK_TIMEOUT for its locks (lock_timeout:
"Abort any statement that waits longer than the specified amount of time
while attempting to acquire a lock on a table, index, row, or other database
object", PostgreSQL documentation, Client Connection Defaults, Statement
Behavior), inside a savepoint: one that times out, or is chosen as a
deadlock's victim, is rolled back alone and its locks with it, the writes
queued behind it are granted at once, and the next attempt asks again at
the back of the queue, ATTEMPTS in all (about a minute). No attempt sleeps:
alembic/env.py runs every pending migration in one transaction, so the locks
an earlier migration of the same run took are held between two attempts,
and the time between them is spent waiting for the lock, never idle with
them held. The timeout is this migration's only: set inside the savepoint
and given back to the transaction's own when it is released, so the other
migrations of the run wait as they always did. When every attempt timed
out, the run fails, nothing of it is kept, the image's start stops
(deploy/entrypoint.sh, `set -e`), and the container's restart policy
(`restart: unless-stopped`) starts it again.
"""
import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e5c9a2f7d3b1"
down_revision = "d8a4f2c6e1b9"
branch_labels = None
depends_on = None

#: The embedding model's dimension (core/embedding_model.DIMENSIONS), written
#: here as a number: a migration must not change when the code does.
_DIMENSIONS = 768

#: Each attempt waits at most this for its locks, and how many attempts,
#: back to back (the docstring): about a minute in all.
LOCK_TIMEOUT = "5s"
ATTEMPTS = 12
#: SQLSTATEs an attempt is tried again on: lock_not_available (what a
#: lock_timeout raises) and deadlock_detected (the attempt was the victim:
#: its savepoint is rolled back, the other transaction goes on). Any other
#: error is raised at once.
_TRY_AGAIN = ("55P03", "40P01")


def with_the_lock(conn, do) -> None:
    """`do()` inside a savepoint whose lock waits are bounded by
    LOCK_TIMEOUT, attempted up to ATTEMPTS times, back to back, while its
    lock is not available or it lost a deadlock; then the lock_timeout the
    transaction had is given back. Any other error, or the last attempt's,
    is raised.

    Read without a database (`alembic upgrade ... --sql`, the dry reading
    before a deploy: on 18 Nov 2026 it stopped here, at the SHOW and the
    savepoint, which need a connection): the statements one attempt runs,
    its lock wait bounded, then the server's default back (what a SET of
    the session had made it, a script cannot know). The attempts again are
    this function's, of a run; a script has none."""
    from sqlalchemy.engine.mock import MockConnection
    from sqlalchemy.exc import OperationalError

    # alembic --sql hands the migration a MockConnection (alembic's
    # runtime/migration.py, as_sql): its statements are written, not run.
    if isinstance(conn, MockConnection):
        op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        do()
        op.execute("SET LOCAL lock_timeout TO DEFAULT")
        return
    before = conn.exec_driver_sql("SHOW lock_timeout").scalar()
    for attempt in range(1, ATTEMPTS + 1):
        point = conn.begin_nested()
        conn.exec_driver_sql(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        try:
            do()
        except OperationalError as e:
            point.rollback()
            code = getattr(e.orig, "pgcode", None) or getattr(e.orig, "sqlstate", None)
            if code not in _TRY_AGAIN or attempt == ATTEMPTS:
                raise
            continue
        point.commit()
        conn.exec_driver_sql(f"SET LOCAL lock_timeout = '{before}'")
        return


def upgrade() -> None:
    with_the_lock(op.get_bind(), _create)


def _create() -> None:
    op.create_table(
        "agent_memory_windows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("memory_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_memories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start_char", sa.Integer(), nullable=False),
        sa.Column("end_char", sa.Integer(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(_DIMENSIONS), nullable=True),
        sa.Column("embedding_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("embedding_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding_next_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_index("uq_agent_memory_windows_ordinal", "agent_memory_windows",
                    ["memory_id", "ordinal"], unique=True)
    op.create_index("idx_agent_memory_windows_tenant", "agent_memory_windows", ["tenant_id"])
    op.execute("SET max_parallel_maintenance_workers = 0")
    op.create_index("idx_agent_memory_windows_embedding_hnsw", "agent_memory_windows",
                    ["embedding"], postgresql_using="hnsw",
                    postgresql_ops={"embedding": "vector_cosine_ops"})
    op.create_index("idx_agent_memory_windows_vector_waiting", "agent_memory_windows",
                    ["embedding_next_at"],
                    postgresql_where=sa.text("embedding_status = 'pending'"))


def downgrade() -> None:
    with_the_lock(op.get_bind(), lambda: op.drop_table("agent_memory_windows"))
