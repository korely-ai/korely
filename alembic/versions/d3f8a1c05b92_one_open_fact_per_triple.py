"""one open fact per triple: the database arbitrates the duplicate

Revision ID: d3f8a1c05b92
Revises: c1a7f3e94d20

Five identical writes sent together produced five identical facts, all
open. In sequence they produced one: the dedup works, but it is a
read-then-write, and under concurrency the workers all look at once, find
nothing, and all insert.

A tester saw it one time in five, and the worst case is an idle machine: with
the queue already full the workers serialise themselves and the defect
disappears. That is, it shows up when nobody is watching.

A race is not closed by looking harder, it is closed by taking the decision
away from whoever reads. The index makes the database the arbiter: two equal
inserts cannot coexist, the second is refused, and whoever receives the
refusal knows somebody else has already written that fact.

`NULLS NOT DISTINCT` is the part that matters and the reason this needs
Postgres 15 or later. Without it two NULLs are not considered equal:
`project_id`, `end_user_id` and `agent_id` are often NULL, and an ordinary
index would never see a duplicate in exactly the most common cases.

Partial on `invalid_at IS NULL`: the constraint is on what is true NOW. The
history must be able to hold the same fact many times, opened and closed and
reopened, and that is the point of the product.
"""
from alembic import op

revision = 'd3f8a1c05b92'
down_revision = 'c1a7f3e94d20'
branch_labels = None
depends_on = None

INDEX = "uq_agent_facts_one_open_per_triple"


def upgrade() -> None:
    # Duplicates that already exist are closed first, or the index cannot be
    # created. The oldest is kept, being the one the others would have reused
    # had there been no race, and the others are invalidated pointing at it:
    # the history stays readable and `as_of` keeps answering.
    op.execute("""
        WITH ranked AS (
            SELECT id,
                   first_value(id) OVER w AS keep,
                   row_number()   OVER w AS n
            FROM agent_facts
            WHERE invalid_at IS NULL
            WINDOW w AS (
                PARTITION BY tenant_id, project_id, end_user_id, agent_id,
                             subject_label, predicate_canonical, object_label
                ORDER BY created_at, id
            )
        )
        UPDATE agent_facts f
           SET invalid_at = now(),
               invalidated_by_id = r.keep
          FROM ranked r
         WHERE f.id = r.id AND r.n > 1
    """)
    op.execute(f"""
        CREATE UNIQUE INDEX {INDEX}
            ON agent_facts (tenant_id, project_id, end_user_id, agent_id,
                            subject_label, predicate_canonical, object_label)
            NULLS NOT DISTINCT
            WHERE invalid_at IS NULL
    """)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
