"""the vocabulary of relations is one per end user of a project

Revision ID: e7d1b3f9a2c6
Revises: c45a7e2b9d13
Create Date: 2026-10-18 23:00:00

The rule of 18 Oct 2026: every end user is a thing of its own, every project
is a thing of its own. A relation of the vocabulary carried a value of the
facts it was learned from (`example_object`, and the `centroid` made of it)
and decisions taken on them (whether it holds one value, its family, its
name), and the vocabulary was the project's (d6a2f8c4e1b7; the account's
before): one end user's value went into the prompts written for another, and
one end user's first conflict decided how the others' facts are judged (the
audit of 18 Oct 2026).

Each end user's vocabulary is rebuilt here from that end user's own facts,
and the shared rows are dropped:
- a relation for every relation name the end user's facts carry (their facts
  keep their names: nothing about a fact changes), its example the object of
  the end user's oldest fact of it, its family the one their own facts
  carry, how many facts of theirs use it;
- whether it holds one value: undecided, voted again from that end user's own
  values the first time two of their facts meet (the lazy votes,
  predicates.LAZY_SINGLE_VALUE): the old vote was taken on somebody else's;
- no centroid: the vector drain computes it from the end user's own example
  (predicates.fill_missing_centroids);
- the phrases: each phrase the end user's own facts were written with and the
  relation it went to, and each relation's own name. A phrase written in
  letters outside ASCII is not carried (its form is computed by code the
  migration does not run): the next time it comes it is asked again, among
  that end user's relations.
A relation no fact uses any more goes: nobody's facts are behind it. Nothing
is asked of a model here.

The downgrade keeps, for each project, the oldest row of each relation and
of each phrase, and puts the per-project keys back.
"""
from alembic import op

revision = "e7d1b3f9a2c6"
down_revision = "c45a7e2b9d13"
branch_labels = None
depends_on = None

# predicate_forms.canonicalize_predicate on ASCII, in SQL: lowercase, every run of
# what is not a letter or a digit one underscore, none at the ends.
_FORM = ("left(trim(both '_' from regexp_replace(lower(f.predicate_raw), '[^a-z0-9]+', '_', 'g')), 200)")


def upgrade() -> None:
    op.execute("""
        ALTER TABLE predicate_clusters ADD COLUMN end_user_id varchar(255);
        ALTER TABLE predicate_aliases ADD COLUMN end_user_id varchar(255);
        DROP INDEX uq_predicate_clusters_project_canonical;
        DROP INDEX uq_predicate_aliases_project_surface;
        DELETE FROM predicate_aliases;
        DELETE FROM predicate_clusters;
        CREATE UNIQUE INDEX uq_predicate_clusters_user_canonical
            ON predicate_clusters (tenant_id, project_id, end_user_id, canonical) NULLS NOT DISTINCT;
        CREATE UNIQUE INDEX uq_predicate_aliases_user_surface
            ON predicate_aliases (tenant_id, project_id, end_user_id, surface) NULLS NOT DISTINCT;
    """)
    op.execute("""
        INSERT INTO predicate_clusters (id, tenant_id, project_id, end_user_id, canonical,
                                        is_functional, family, example_object, member_count)
        SELECT gen_random_uuid(), tenant_id, project_id, end_user_id, predicate_canonical,
               NULL, family, left(example, 500), how_many
          FROM (
            SELECT f.tenant_id, f.project_id, f.end_user_id, f.predicate_canonical,
                   (array_agg(f.object_label ORDER BY f.created_at, f.id))[1] AS example,
                   mode() WITHIN GROUP (ORDER BY f.predicate_family) AS family,
                   count(*) AS how_many
              FROM agent_facts f
             WHERE f.predicate_canonical IS NOT NULL AND f.predicate_canonical <> ''
             GROUP BY f.tenant_id, f.project_id, f.end_user_id, f.predicate_canonical
          ) r;
    """)
    # Each relation's own name, then the phrases its end user's facts were
    # written with (the newest fact's relation when one phrase went to two).
    op.execute("""
        INSERT INTO predicate_aliases (tenant_id, project_id, end_user_id, surface, cluster_id)
        SELECT tenant_id, project_id, end_user_id, canonical, id FROM predicate_clusters;
    """)
    op.execute(f"""
        INSERT INTO predicate_aliases (tenant_id, project_id, end_user_id, surface, cluster_id)
        SELECT DISTINCT ON (f.tenant_id, f.project_id, f.end_user_id, {_FORM})
               f.tenant_id, f.project_id, f.end_user_id, {_FORM}, c.id
          FROM agent_facts f
          JOIN predicate_clusters c
            ON c.tenant_id = f.tenant_id
           AND c.project_id IS NOT DISTINCT FROM f.project_id
           AND c.end_user_id IS NOT DISTINCT FROM f.end_user_id
           AND c.canonical = f.predicate_canonical
         WHERE octet_length(f.predicate_raw) = char_length(f.predicate_raw)
           AND length({_FORM}) > 2
         ORDER BY f.tenant_id, f.project_id, f.end_user_id, {_FORM}, f.created_at DESC, f.id DESC
        ON CONFLICT DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX uq_predicate_clusters_user_canonical;
        DROP INDEX uq_predicate_aliases_user_surface;
        CREATE TEMP TABLE prima AS
        SELECT DISTINCT ON (tenant_id, project_id, canonical) id, tenant_id, project_id, canonical
          FROM predicate_clusters ORDER BY tenant_id, project_id, canonical, created_at, id;
        CREATE TEMP TABLE mappa AS
        SELECT c.id AS da, p.id AS a
          FROM predicate_clusters c JOIN prima p
            ON p.tenant_id = c.tenant_id AND p.project_id IS NOT DISTINCT FROM c.project_id
           AND p.canonical = c.canonical;
        CREATE TEMP TABLE frasi AS
        SELECT DISTINCT ON (v.tenant_id, v.project_id, v.surface)
               v.tenant_id, v.project_id, v.surface, m.a AS cluster_id, v.created_at
          FROM predicate_aliases v JOIN mappa m ON m.da = v.cluster_id
         ORDER BY v.tenant_id, v.project_id, v.surface, v.created_at, v.cluster_id;
        DELETE FROM predicate_aliases;
        DELETE FROM predicate_clusters c WHERE NOT EXISTS (SELECT 1 FROM prima p WHERE p.id = c.id);
        INSERT INTO predicate_aliases (tenant_id, project_id, surface, cluster_id, created_at)
        SELECT tenant_id, project_id, surface, cluster_id, created_at FROM frasi;
        ALTER TABLE predicate_clusters DROP COLUMN end_user_id;
        ALTER TABLE predicate_aliases DROP COLUMN end_user_id;
        CREATE UNIQUE INDEX uq_predicate_clusters_project_canonical
            ON predicate_clusters (tenant_id, project_id, canonical) NULLS NOT DISTINCT;
        CREATE UNIQUE INDEX uq_predicate_aliases_project_surface
            ON predicate_aliases (tenant_id, project_id, surface) NULLS NOT DISTINCT;
    """)
