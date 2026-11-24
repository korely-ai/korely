"""a link between two subjects of the judge's wall, and the closings it made

Revision ID: a9d4c7e2f1b6
Revises: e5c9a2f7d3b1
Create Date: 2026-11-23 20:00:00

memory_core 6.20.0 (`objects.review_subjects`, off by default: `manage
review-subjects`, services/facts/subjects.SAME_SUBJECT_REVIEW):

(a) `agent_object_links.scope`: "memory" (the default, every row written
until now: two objects of one memory, read by the reads) or "wall" (two
subjects of a memory with no end user, in the judge's wall: the account, the
project, no end user, every agent; `end_user_id` and `agent_id` NULL). Its
own column, not an empty `agent_id`, which is already the memory with no
agent. The unique name of a memory includes it, so a wall's link and the
no-agent memory's link of one name are two rows.

(b) `agent_fact_closure_links`: a closing the judge made only through such a
link (the fact closed, the link). `fact_id` ON DELETE CASCADE; `link_id` ON
DELETE NO ACTION, checked at the end of each statement: a statement that
deletes a link with closings still recorded on it fails, so a link leaves
only by the door that gives its closings back (memory_core
`objects.remove_link`), while an erasure that takes the facts and the links
together passes.

Nothing is filled here: the table stays empty and no row is "wall" until
somebody applies a review of subjects.

The downgrade takes the "wall" rows away through their closings first: a
closing recorded on a link is given back only by the product
(facts/subjects.remove), which a migration does not run, so the downgrade
refuses while any closing is recorded (remove those links first, from the
dashboard or with DELETE /admin/object-links/{id}); then the table, the
"wall" rows, the index and the column.

The foreign keys take a SHARE ROW EXCLUSIVE lock on `agent_facts` and on
`agent_object_links`, and the new column an ACCESS EXCLUSIVE one on the
latter, until the migration commits: each attempt waits at most
LOCK_TIMEOUT for its locks, inside a savepoint, ATTEMPTS in all, as
e5c9a2f7d3b1 (whose docstring says why and how), with its helper.
"""
import importlib.util
import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a9d4c7e2f1b6"
down_revision = "e5c9a2f7d3b1"
branch_labels = None
depends_on = None


def _with_the_lock(conn, do) -> None:
    """e5c9a2f7d3b1's `with_the_lock`, the one helper of the bounded lock
    waits, loaded from its file (a migration's module is not importable by
    name)."""
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "_e5c9a2f7d3b1", os.path.join(here, "e5c9a2f7d3b1_a_long_memory_has_a_vector_for_each_window.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.with_the_lock(conn, do)


def upgrade() -> None:
    _with_the_lock(op.get_bind(), _create)


def _create() -> None:
    op.add_column("agent_object_links",
                  sa.Column("scope", sa.String(10), nullable=False, server_default="memory"))
    op.create_check_constraint("ck_agent_object_links_scope", "agent_object_links",
                               "scope IN ('memory', 'wall')")
    op.drop_index("uq_agent_object_links_name_per_memory", table_name="agent_object_links")
    op.create_index("uq_agent_object_links_name_per_memory", "agent_object_links",
                    ["tenant_id", "project_id", "end_user_id", "agent_id", "scope",
                     sa.text("lower(name_label)")],
                    unique=True, postgresql_nulls_not_distinct=True)
    op.create_table(
        "agent_fact_closure_links",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fact_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_facts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("link_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_object_links.id", ondelete="NO ACTION"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_index("idx_agent_fact_closure_links_link", "agent_fact_closure_links", ["link_id"])
    op.create_index("ix_agent_fact_closure_links_tenant_id", "agent_fact_closure_links", ["tenant_id"])


def downgrade() -> None:
    conn = op.get_bind()
    held = conn.exec_driver_sql("SELECT count(*) FROM agent_fact_closure_links").scalar()
    if held:
        raise RuntimeError(
            f"{held} closings are recorded on links between subjects: remove those links first "
            "(DELETE /admin/object-links/{id}), which gives the closings back, then downgrade")
    _with_the_lock(conn, _drop)


def _drop() -> None:
    op.drop_table("agent_fact_closure_links")
    op.execute("DELETE FROM agent_object_links WHERE scope = 'wall'")
    op.drop_index("uq_agent_object_links_name_per_memory", table_name="agent_object_links")
    op.create_index("uq_agent_object_links_name_per_memory", "agent_object_links",
                    ["tenant_id", "project_id", "end_user_id", "agent_id", sa.text("lower(name_label)")],
                    unique=True, postgresql_nulls_not_distinct=True)
    op.drop_constraint("ck_agent_object_links_scope", "agent_object_links", type_="check")
    op.drop_column("agent_object_links", "scope")
