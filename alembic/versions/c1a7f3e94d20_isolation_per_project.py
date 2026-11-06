"""The project becomes a wall, not a label.

Until now `project_id` existed on agent_memories and agent_facts and nothing
read it: the columns were added ahead of time so that L2 would be purely
additive. This is L2. From here on an API key sees only the project it
belongs to.

The migration has to do one thing well: not make data disappear for whoever
upgrades. The risk is precise. If the rows stay NULL and the keys land on a
project, every key stops seeing everything that existed before the upgrade,
and the install looks emptied. So the two halves move together, in one
transaction:

  1. every account without a default project gets one, called Default
  2. every memory, fact and audit row without a project moves to the default
  3. every key without a project moves to the default

Afterwards keys and rows agree, and an install with a single project behaves
exactly as before. Anyone who had already created several finds their keys
already assigned and the historical data in Default, which is the only honest
attribution: nobody ever recorded which project a row was written from, and
inventing it would be worse than admitting it.

Revision ID: c1a7f3e94d20
Revises: 99bb3d378f32
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c1a7f3e94d20'
down_revision = '99bb3d378f32'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The batch is the only table that really lacked the column: the job is
    # imported by a worker long after the request that carried the key has
    # finished, so the project has to be on the row.
    op.add_column(
        "agent_batch_jobs",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_batch_jobs_project", "agent_batch_jobs", "agent_projects",
        ["project_id"], ["id"], ondelete="SET NULL",
    )

    # 1. a default project for every account that does not have one.
    op.execute("""
        INSERT INTO agent_projects (id, tenant_id, name, slug, is_default, created_at)
        SELECT gen_random_uuid(), t.id, 'Default', 'default', true, now()
        FROM tenants t
        WHERE NOT EXISTS (
            SELECT 1 FROM agent_projects p
            WHERE p.tenant_id = t.id AND p.is_default
        )
    """)

    # 2. data that never had a project goes to the default one.
    for table in ("agent_memories", "agent_facts", "agent_forget_audit"):
        op.execute(f"""
            UPDATE {table} x
            SET project_id = p.id
            FROM agent_projects p
            WHERE p.tenant_id = x.tenant_id AND p.is_default
              AND x.project_id IS NULL
        """)

    # 3. and the keys with them, at the same moment. This is the point: if the
    #    rows moved and the keys did not, or the other way round, the install
    #    would answer empty to the first GET after the upgrade.
    op.execute("""
        UPDATE agent_api_keys k
        SET project_id = p.id
        FROM agent_projects p
        WHERE p.tenant_id = k.tenant_id AND p.is_default
          AND k.project_id IS NULL
    """)

    # Reads almost always filter on (tenant, project) together; the existing
    # indexes on agent_memories and agent_facts are already that pair.
    op.create_index("idx_agent_batch_project", "agent_batch_jobs", ["project_id"])


def downgrade() -> None:
    # Only the new column goes back. The project_ids filled in at steps 2 and 3
    # stay: they are correct without the wall too, and clearing them would throw
    # away an assignment an operator may have corrected by hand in the meantime.
    op.drop_index("idx_agent_batch_project", table_name="agent_batch_jobs")
    op.drop_constraint("fk_agent_batch_jobs_project", "agent_batch_jobs", type_="foreignkey")
    op.drop_column("agent_batch_jobs", "project_id")
