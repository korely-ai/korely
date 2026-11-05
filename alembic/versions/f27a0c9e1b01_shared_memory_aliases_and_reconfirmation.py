"""the shared memory: aliases, reconfirmation, tense of the fact, family of the relation

Revision ID: f27a0c9e1b01
Revises: e7b21c4a9f30
Create Date: 2026-09-27

The same columns and the same table as the engine's earlier products had,
because the rules that write them are now one body of code
(`services/memory_core/`):

  * `agent_facts`: canonical name of subject and object (after aliases), tense
    of the fact as read in the text (current|past|planned), last confirmation,
    how many memories state it and which, family of the relation;
  * `agent_entity_aliases`: an old name -> today's name, per memory
    (tenant + project + end user + agent);
  * `predicate_clusters.family`: the family of a learned relation;
  * the "one open fact per triple" index moves to the lower-cased CANONICAL
    names. First the open duplicates the change brings out ("Maria" and
    "maria") are merged into the oldest, which inherits sources, count and
    last confirmation; the others are removed (nothing stopped being true)
    after moving the `invalidated_by_id` chains onto it.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f27a0c9e1b01"
down_revision = "e7b21c4a9f30"
branch_labels = None
depends_on = None

INDEX = "uq_agent_facts_one_open_per_triple"
KEY = ("tenant_id, project_id, end_user_id, agent_id, "
          "lower(subject_canonical), predicate_canonical, lower(object_canonical)")


def upgrade() -> None:
    op.add_column("predicate_clusters", sa.Column("family", sa.String(20), nullable=True))

    for col in (
        sa.Column("subject_canonical", sa.String(200), nullable=True),
        sa.Column("object_canonical", sa.String(500), nullable=True),
        sa.Column("tense", sa.String(10), nullable=False, server_default="current"),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observation_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_memory_ids", sa.JSON(), nullable=True),
        sa.Column("predicate_family", sa.String(20), nullable=True),
    ):
        op.add_column("agent_facts", col)
    op.execute("""
        UPDATE agent_facts
           SET subject_canonical = subject_label,
               object_canonical  = object_label,
               last_confirmed_at = created_at,
               source_memory_ids = CASE WHEN source_memory_id IS NULL THEN NULL
                                        ELSE json_build_array(source_memory_id::text) END
         WHERE subject_canonical IS NULL
    """)

    op.create_table(
        "agent_entity_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("end_user_id", sa.String(255), nullable=True),
        sa.Column("agent_id", sa.String(255), nullable=True),
        sa.Column("alias_lower", sa.String(200), nullable=False),
        sa.Column("alias_label", sa.String(200), nullable=False),
        sa.Column("canonical_label", sa.String(200), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="user"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("source_fact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_facts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_agent_entity_aliases_tenant_id", "agent_entity_aliases", ["tenant_id"])
    op.execute("""
        CREATE UNIQUE INDEX uq_agent_entity_aliases_alias_per_memoria
            ON agent_entity_aliases (tenant_id, project_id, end_user_id, agent_id, alias_lower)
            NULLS NOT DISTINCT
    """)

    op.execute(f"""
        CREATE TEMP TABLE doppioni AS
        SELECT tenant_id, project_id, end_user_id, agent_id,
               lower(subject_canonical) AS s, predicate_canonical AS p, lower(object_canonical) AS o,
               count(*) AS tot, max(created_at) AS ultimo,
               (array_agg(id ORDER BY created_at, id))[1] AS keep,
               array_remove(array_agg(DISTINCT source_memory_id::text), NULL) AS sources
          FROM agent_facts
         WHERE invalid_at IS NULL
         GROUP BY {KEY}
        HAVING count(*) > 1
    """)
    op.execute("""
        CREATE TEMP TABLE perdenti AS
        SELECT f.id, d.keep
          FROM agent_facts f
          JOIN doppioni d
            ON f.tenant_id = d.tenant_id
           AND f.project_id  IS NOT DISTINCT FROM d.project_id
           AND f.end_user_id IS NOT DISTINCT FROM d.end_user_id
           AND f.agent_id    IS NOT DISTINCT FROM d.agent_id
           AND lower(f.subject_canonical) = d.s
           AND f.predicate_canonical = d.p
           AND lower(f.object_canonical) = d.o
         WHERE f.invalid_at IS NULL AND f.id <> d.keep
    """)
    op.execute("""
        UPDATE agent_facts f
           SET observation_count = d.tot,
               last_confirmed_at = d.ultimo,
               source_memory_ids = CASE WHEN cardinality(d.sources) = 0 THEN NULL ELSE to_json(d.sources) END
          FROM doppioni d
         WHERE f.id = d.keep
    """)
    op.execute("UPDATE agent_facts f SET invalidated_by_id = p.keep FROM perdenti p WHERE f.invalidated_by_id = p.id")
    op.execute("DELETE FROM agent_facts f USING perdenti p WHERE f.id = p.id")
    op.execute("DROP TABLE perdenti")
    op.execute("DROP TABLE doppioni")

    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"""
        CREATE UNIQUE INDEX {INDEX}
            ON agent_facts ({KEY})
            NULLS NOT DISTINCT
            WHERE invalid_at IS NULL
    """)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"""
        CREATE UNIQUE INDEX {INDEX}
            ON agent_facts (tenant_id, project_id, end_user_id, agent_id,
                            subject_label, predicate_canonical, object_label)
            NULLS NOT DISTINCT
            WHERE invalid_at IS NULL
    """)
    op.execute("DROP INDEX IF EXISTS uq_agent_entity_aliases_alias_per_memoria")
    op.drop_index("ix_agent_entity_aliases_tenant_id", table_name="agent_entity_aliases")
    op.drop_table("agent_entity_aliases")
    for col in ("predicate_family", "source_memory_ids", "observation_count", "last_confirmed_at",
                "tense", "object_canonical", "subject_canonical"):
        op.drop_column("agent_facts", col)
    op.drop_column("predicate_clusters", "family")
