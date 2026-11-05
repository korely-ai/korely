"""an index on the vectors, which was missing

Revision ID: 4565f3e40de9
Revises: 1069c7080e44
Create Date: 2026-09-12 10:59:28.659426
"""
from alembic import op

# Always imported: autogenerate emits pgvector.sqlalchemy.vector.VECTOR(...) for
# embedding columns but does not add the import itself, and a migration that
# raises NameError takes the container down before the application starts. The
# unused-import warning is cheaper than a boot loop.
import pgvector.sqlalchemy  # noqa: F401


revision = '4565f3e40de9'
down_revision = '1069c7080e44'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """An HNSW index on both vector columns.

    There was none, anywhere, and nothing noticed because every test until
    today ran with a few dozen rows. Measured on a Hetzner CPX22 with 50,000
    memories: one semantic search took 287ms as a sequential scan and 7.8ms
    through the index. Thirty-seven times, and the difference grows with the
    row count, because one of them is linear and the other is not.

    Serial, not parallel. pgvector's parallel build asks for shared memory,
    Postgres in Docker gets 64MB of /dev/shm by default, and the build fails
    with "could not resize shared memory segment" - which reads like a full
    disk and is not one. The compose file now asks for 1GB of shm as well, but
    this migration cannot rely on somebody else's compose file.

    Cost on an existing install: the index is built at boot, before the server
    starts serving, so nothing is waiting on it. Fifty thousand memories took
    53 seconds on two shared vCPUs. A much larger store will take longer, and
    that time is added to one restart, once.
    """
    # Build settings, for this session only.
    op.execute("SET max_parallel_maintenance_workers = 0")
    op.execute("SET maintenance_work_mem = '512MB'")

    # IF NOT EXISTS: an operator who already added one by hand should not have
    # their upgrade fail for having been ahead of us.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_memories_embedding_hnsw "
        "ON agent_memories USING hnsw (embedding vector_cosine_ops)"
    )
    # The facts carry embeddings too, and `/v1/context` ranks them by cosine
    # distance on every read. Same scan, same fix.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_facts_embedding_hnsw "
        "ON agent_facts USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_facts_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS idx_agent_memories_embedding_hnsw")
