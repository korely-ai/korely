"""which fact a named reader was handed, and when

Revision ID: e4b7c1d9a2f6
Revises: c5e8b3f1a9d4
Create Date: 2026-11-08 18:30:00

Agent notices, part one (1.0.15): `agent_read_marks`, one row per account,
project, reader and fact, written only by a read of what is true now that
names its reader (`reader` on GET /v1/context, GET /v1/facts and their MCP
twins, services/read_marks.py). No text: ids, the reader's name, the fact's
end user and three times. Empty at the upgrade: no read named a reader
before this revision.

The fact's foreign key is ON DELETE CASCADE (a deleted fact takes the rows
that say it was read), and so are the project's and the account's. An
erasure of the end user deletes their rows (services/end_user_places.py).
The worker deletes a row READ_MARKS_RETENTION_DAYS (90) after its last read.

The unique index is NULLS NOT DISTINCT (Postgres 15+), as the facts' own:
a project of NULL is one project.

The downgrade drops the table: nothing but the notices reads it.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e4b7c1d9a2f6"
down_revision = "c5e8b3f1a9d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_read_marks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("reader", sa.String(255), nullable=False),
        sa.Column("fact_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_facts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("end_user_id", sa.String(255), nullable=True),
        sa.Column("first_read_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("last_read_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("uq_agent_read_marks_reader_fact", "agent_read_marks",
                    ["tenant_id", "project_id", "reader", "fact_id"],
                    unique=True, postgresql_nulls_not_distinct=True)
    op.create_index("idx_agent_read_marks_reader_read", "agent_read_marks",
                    ["tenant_id", "project_id", "reader", "last_read_at"])
    op.create_index("idx_agent_read_marks_fact", "agent_read_marks", ["fact_id"])


def downgrade() -> None:
    op.drop_table("agent_read_marks")
