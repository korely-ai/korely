"""a model call's cost row names the project it worked for

Revision ID: a3d7f9c1e5b2
Revises: c4e9a7d2b5f8
Create Date: 2026-11-26 13:30:00

`agent_llm_usage.project_id`, NULL by default, a foreign key to
`agent_projects` with ON DELETE SET NULL (a deleted project's rows move to
the Default project first, services/projects._move_what_is_left), and the
index the usage report's `by_project` reads, (tenant_id, project_id, ts),
beside the two of the account and of the agent. Since 1.0.41 (26 Nov 2026)
the model usage of an account is counted project by project.

Existing rows get no project: the calls made before were not attributed to
one, and the report counts them as "no project", so its projects still add
up to the account's totals. Adding a nullable column with no default
changes no row (PostgreSQL 11 and later), but it and the foreign key take
locks on the table: each attempt to take them waits at most LOCK_TIMEOUT,
inside a savepoint, with e5c9a2f7d3b1's helper, as c4e9a7d2b5f8 does. The
index is built after, in the same transaction, so the locks are held until
the migration commits: measured about 1.0 s on 2 million rows (the skeptic's
run on the test Postgres, 26 Nov 2026), on a table the retention keeps to
400 days.

The downgrade drops the index, the constraint and the column; the release
before reads the account and the agent only, as it always did.
"""
import importlib.util
import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a3d7f9c1e5b2"
down_revision = "c4e9a7d2b5f8"
branch_labels = None
depends_on = None

# The name PostgreSQL gives the constraint `create_all` makes from the model.
_FK = "agent_llm_usage_project_id_fkey"
_INDEX = "idx_llm_usage_tenant_project_ts"


def _with_the_lock(conn, do) -> None:
    """e5c9a2f7d3b1's `with_the_lock`, the one helper of the bounded lock
    waits, loaded from its file (a migration's module is not importable by
    name)."""
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "e5c9a2f7d3b1", os.path.join(here, "e5c9a2f7d3b1_a_long_memory_has_a_vector_for_each_window.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.with_the_lock(conn, do)


def upgrade() -> None:
    def add() -> None:
        op.add_column("agent_llm_usage",
                      sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(_FK, "agent_llm_usage", "agent_projects", ["project_id"], ["id"],
                              ondelete="SET NULL")

    _with_the_lock(op.get_bind(), add)
    op.create_index(_INDEX, "agent_llm_usage", ["tenant_id", "project_id", "ts"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="agent_llm_usage")
    op.drop_constraint(_FK, "agent_llm_usage", type_="foreignkey")
    op.drop_column("agent_llm_usage", "project_id")
