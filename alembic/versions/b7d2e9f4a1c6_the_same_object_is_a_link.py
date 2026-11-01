"""the same object in two names is a link, not an alias

Revision ID: b7d2e9f4a1c6
Revises: a4e8c2f6d9b3
Create Date: 2026-10-30 18:00:00

memory_core 6.1.0 (`objects`, off by default): when the writer of the
project's region judges that a new object of a memory is the same thing as
one the memory holds ("Italy" and "Italia"), the union is a row of
`agent_object_links`, which the reads resolve. No fact is rewritten, merged
or reconfirmed: both objects keep their facts, labels, dates, tense,
evidence and sources, and deleting the row puts the memory back exactly as
it was (services/facts/links.py). An alias (`agent_entity_aliases`) is a
different thing and keeps its behaviour: it rewrites the facts' canonical
names and merges duplicates.

`source_memory_id` (the memory that named the linked object) and
`canonical_memory_id` (the memory of the fact it was compared with) are ON
DELETE CASCADE: the row holds the writer's descriptions of both, words
derived from those memories, and goes with either. The erasure also deletes
it explicitly (services/facts/erasure.py).

The name is unique in its memory, compared lowercased by the database
(Postgres 15+, NULLS NOT DISTINCT as the aliases' index). `family` is the
relation family the link was decided in, the only one the reads apply it to
(changed in place on 30 Oct 2026, before any release: the table was empty
everywhere).

The downgrade drops the table: a link only changes what the reads show.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b7d2e9f4a1c6"
down_revision = "a4e8c2f6d9b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_object_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("end_user_id", sa.String(255), nullable=True),
        sa.Column("agent_id", sa.String(255), nullable=True),
        sa.Column("name_label", sa.String(200), nullable=False),
        sa.Column("canonical_label", sa.String(200), nullable=False),
        sa.Column("family", sa.String(20), nullable=False),
        sa.Column("decided_by", sa.String(20), nullable=False),
        sa.Column("source_memory_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_memories.id", ondelete="CASCADE"), nullable=True),
        sa.Column("source_fact_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_facts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("canonical_memory_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_memories.id", ondelete="CASCADE"), nullable=True),
        sa.Column("reason", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_agent_object_links_tenant_id", "agent_object_links", ["tenant_id"])
    op.create_index("uq_agent_object_links_name_per_memory", "agent_object_links",
                    ["tenant_id", "project_id", "end_user_id", "agent_id", sa.text("lower(name_label)")],
                    unique=True, postgresql_nulls_not_distinct=True)
    op.create_index("idx_agent_object_links_source_memory", "agent_object_links", ["source_memory_id"])
    op.create_index("idx_agent_object_links_canonical_memory", "agent_object_links",
                    ["canonical_memory_id"])
    op.create_index("idx_agent_object_links_source_fact", "agent_object_links", ["source_fact_id"])


def downgrade() -> None:
    op.drop_table("agent_object_links")
