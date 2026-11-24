"""a write sent again with its Idempotency-Key is made once

Revision ID: c4e2a9d7b1f3
Revises: b75be07d8d20
Create Date: 2026-11-15 03:00:00

`agent_idempotency_keys`: one row per account, project and
`Idempotency-Key` that POST /v1/memories or POST /v1/batch was sent with
(services/idempotency.py; the IETF draft
draft-ietf-httpapi-idempotency-key-header-08). The key's SHA-256, the
SHA-256 of the checked request, the memory or the job it made, and when it
expires (24 hours). The foreign keys to what it made are checked at the
commit (DEFERRABLE INITIALLY DEFERRED) and go with it (ON DELETE CASCADE).
The table is new and empty: nothing is filled in for the writes made
before.

The downgrade drops the table: a write sent again after it, with a key the
release before took, is a new write, as it was before 1.0.27.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c4e2a9d7b1f3"
down_revision = "b75be07d8d20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_idempotency_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("memory_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_memories.id", ondelete="CASCADE",
                                deferrable=True, initially="DEFERRED"),
                  nullable=True),
        sa.Column("batch_job_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_batch_jobs.id", ondelete="CASCADE",
                                deferrable=True, initially="DEFERRED"),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("(memory_id IS NULL) <> (batch_job_id IS NULL)",
                           name="ck_agent_idempotency_keys_one_target"),
    )
    op.create_index("uq_agent_idempotency_keys_key", "agent_idempotency_keys",
                    ["tenant_id", "project_id", "key_hash"],
                    unique=True, postgresql_nulls_not_distinct=True)
    op.create_index("idx_agent_idempotency_keys_expires", "agent_idempotency_keys",
                    ["expires_at"])
    op.create_index("idx_agent_idempotency_keys_memory", "agent_idempotency_keys",
                    ["memory_id"])
    op.create_index("idx_agent_idempotency_keys_job", "agent_idempotency_keys",
                    ["batch_job_id"])


def downgrade() -> None:
    op.drop_index("idx_agent_idempotency_keys_job", table_name="agent_idempotency_keys")
    op.drop_index("idx_agent_idempotency_keys_memory", table_name="agent_idempotency_keys")
    op.drop_index("idx_agent_idempotency_keys_expires", table_name="agent_idempotency_keys")
    op.drop_index("uq_agent_idempotency_keys_key", table_name="agent_idempotency_keys")
    op.drop_table("agent_idempotency_keys")
