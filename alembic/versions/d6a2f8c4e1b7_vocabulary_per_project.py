"""the vocabulary of relations is one per project

Revision ID: d6a2f8c4e1b7
Revises: c3f5a7e9b1d4
Create Date: 2026-10-18 02:00:00

The code audit of 17 Oct 2026 (P1, the relation vocabulary is per tenant): a
relation's `example_object` is an end user's value, shown to the model next to
the relation, and the vocabulary was the account's, so a value written in a
project confined to the EU went into the prompts of the account's Global
projects, to the Global provider. Facts, memories and candidates were already
behind the project's wall; now the vocabulary is too.

The data is split by use. A relation stays with the project that used it
first (its facts carry its name) and is copied, with its phrases, into every
other project whose facts use it; one no fact uses goes to the account's
Default project. Each copy's example is a value of that project's own facts:
the original example where that project wrote it, else the project's first
value for the relation, and no example where there was none. The vote on
whether the relation holds one value travels with it (a yes or a no, nobody's
data). The centroid does not: it is the vector of the relation's name with its
example, so a copy, and a row whose example changed, keep none, and the vector
drain computes it again from the project's own example
(predicates.fill_missing_centroids; the code audit of 18 Oct 2026: a vector
made from one project's value was copied into the others). Nothing is asked of
a model here.

The downgrade keeps, for each account, the oldest copy of each relation and
the oldest row of each phrase, and puts the account-wide keys back.
"""
from alembic import op

revision = "d6a2f8c4e1b7"
down_revision = "c3f5a7e9b1d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE predicate_clusters
            ADD COLUMN project_id uuid REFERENCES agent_projects(id) ON DELETE CASCADE;
        ALTER TABLE predicate_aliases
            ADD COLUMN project_id uuid REFERENCES agent_projects(id) ON DELETE CASCADE;
        ALTER TABLE predicate_aliases DROP CONSTRAINT predicate_aliases_pkey;
        DROP INDEX uq_predicate_clusters_tenant_canonical;
    """)

    # Which projects use each relation, and when first.
    op.execute("""
        CREATE TEMP TABLE esempi_prima AS
        SELECT id, example_object FROM predicate_clusters;

        CREATE TEMP TABLE uso AS
        SELECT c.id AS cluster_id, f.project_id, min(f.created_at) AS primo
          FROM predicate_clusters c
          JOIN agent_facts f ON f.tenant_id = c.tenant_id AND f.predicate_canonical = c.canonical
         GROUP BY c.id, f.project_id;

        CREATE TEMP TABLE casa AS
        SELECT DISTINCT ON (cluster_id) cluster_id, project_id
          FROM uso ORDER BY cluster_id, primo, project_id NULLS FIRST;

        UPDATE predicate_clusters c SET project_id = h.project_id
          FROM casa h WHERE h.cluster_id = c.id;

        -- A relation no fact uses: the account's Default project.
        UPDATE predicate_clusters c SET project_id = p.id
          FROM agent_projects p
         WHERE p.tenant_id = c.tenant_id AND p.is_default
           AND NOT EXISTS (SELECT 1 FROM uso u WHERE u.cluster_id = c.id);

        UPDATE predicate_aliases a SET project_id = c.project_id
          FROM predicate_clusters c WHERE c.id = a.cluster_id;

        -- A copy for every other project that uses the relation.
        CREATE TEMP TABLE copie AS
        SELECT u.cluster_id, u.project_id, gen_random_uuid() AS nuovo
          FROM uso u JOIN casa h USING (cluster_id)
         WHERE u.project_id IS DISTINCT FROM h.project_id;

        INSERT INTO predicate_clusters (id, tenant_id, project_id, canonical, centroid,
                                        is_functional, family, example_object, member_count,
                                        created_at)
        SELECT k.nuovo, c.tenant_id, k.project_id, c.canonical, NULL, c.is_functional,
               c.family, c.example_object, c.member_count, c.created_at
          FROM copie k JOIN predicate_clusters c ON c.id = k.cluster_id;

        INSERT INTO predicate_aliases (tenant_id, project_id, surface, cluster_id, created_at)
        SELECT a.tenant_id, k.project_id, a.surface, k.nuovo, a.created_at
          FROM copie k JOIN predicate_aliases a ON a.cluster_id = k.cluster_id;

        -- Each project's example is its own: the original where this project
        -- wrote it, else its first value for the relation.
        UPDATE predicate_clusters c SET example_object = (
            SELECT left(f.object_label, 500) FROM agent_facts f
             WHERE f.tenant_id = c.tenant_id AND f.predicate_canonical = c.canonical
               AND f.project_id IS NOT DISTINCT FROM c.project_id
             ORDER BY (f.object_label = c.example_object) DESC, f.created_at, f.id
             LIMIT 1)
         WHERE c.example_object IS NOT NULL
           AND EXISTS (SELECT 1 FROM agent_facts f
                        WHERE f.tenant_id = c.tenant_id AND f.predicate_canonical = c.canonical
                          AND f.project_id IS NOT DISTINCT FROM c.project_id);
        -- A relation no fact of its project uses keeps no example: it would be
        -- another project's value, or one already erased.
        UPDATE predicate_clusters c SET example_object = NULL
         WHERE c.example_object IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM agent_facts f
                            WHERE f.tenant_id = c.tenant_id AND f.predicate_canonical = c.canonical
                              AND f.project_id IS NOT DISTINCT FROM c.project_id);

        -- A centroid is the vector of the name with the example: where the
        -- example is not the one it was made with, it goes.
        UPDATE predicate_clusters c SET centroid = NULL
         WHERE NOT EXISTS (SELECT 1 FROM esempi_prima e
                            WHERE e.id = c.id
                              AND e.example_object IS NOT DISTINCT FROM c.example_object);

        DROP TABLE copie; DROP TABLE casa; DROP TABLE uso; DROP TABLE esempi_prima;
    """)

    op.execute("""
        CREATE UNIQUE INDEX uq_predicate_clusters_project_canonical
            ON predicate_clusters (tenant_id, project_id, canonical) NULLS NOT DISTINCT;
        CREATE INDEX ix_predicate_clusters_project_id ON predicate_clusters (project_id);
        ALTER TABLE predicate_aliases ADD PRIMARY KEY (surface, cluster_id);
        CREATE UNIQUE INDEX uq_predicate_aliases_project_surface
            ON predicate_aliases (tenant_id, project_id, surface) NULLS NOT DISTINCT;
        CREATE INDEX ix_predicate_aliases_project_id ON predicate_aliases (project_id);
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX ix_predicate_aliases_project_id;
        DROP INDEX uq_predicate_aliases_project_surface;
        ALTER TABLE predicate_aliases DROP CONSTRAINT predicate_aliases_pkey;
        DROP INDEX ix_predicate_clusters_project_id;
        DROP INDEX uq_predicate_clusters_project_canonical;

        DELETE FROM predicate_clusters c USING predicate_clusters d
         WHERE d.tenant_id = c.tenant_id AND d.canonical = c.canonical
           AND (d.created_at, d.id) < (c.created_at, c.id);
        DELETE FROM predicate_aliases a USING predicate_aliases b
         WHERE b.tenant_id = a.tenant_id AND b.surface = a.surface
           AND (b.created_at, b.cluster_id) < (a.created_at, a.cluster_id);

        ALTER TABLE predicate_aliases ADD PRIMARY KEY (tenant_id, surface);
        CREATE UNIQUE INDEX uq_predicate_clusters_tenant_canonical
            ON predicate_clusters (tenant_id, canonical);
        ALTER TABLE predicate_aliases DROP COLUMN project_id;
        ALTER TABLE predicate_clusters DROP COLUMN project_id;
    """)
