"""the entity aliases of no project go to the default project

Revision ID: a9d3f6c2e8b4
Revises: c7e2a5f9b3d1
Create Date: 2026-10-19 16:00:00

The audit of 19 Oct 2026: until 0.1.40, POST /admin/aliases without a
`project_id` wrote the alias with no project (5860a8e put it in the key's
project from 0.1.41). The migration that moved every row of no project to the
account's Default project (c1a7f3e94d20) ran before this table existed
(f27a0c9e1b01), so those rows stayed where no erasure reaches them: an
end user's erasure through /v1 is behind the key's project, and a project's
erasure behind that project, so the end user's names ("My Wife" -> "Maria")
survived an erasure that answered "permanent", in clear, listed by GET
/admin/aliases.

Moved here as c1a7f3e94d20 moved the memories and the facts: to the account's
Default project, the only honest attribution, since nobody recorded which
project the alias was meant for. Where the Default project already holds the
same alias of the same end user and agent, the unique index
(tenant, project, end user, agent, alias) admits one: the newer decision
(`since`, then `created_at`) stays and the other goes. An account with no
Default project keeps its rows as they are.

The downgrade leaves them in the Default project, as c1a7f3e94d20's does:
they are right there without this revision too, and which rows came from no
project is not recorded.
"""
from alembic import op

revision = "a9d3f6c2e8b4"
down_revision = "c7e2a5f9b3d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The older of each pair that would meet on the unique index first. The
    # index treats NULLs as equal (NULLS NOT DISTINCT), so the end user and
    # the agent compare the same way.
    op.execute("""
        WITH pairs AS (
            SELECT n.id AS legacy, d.id AS there,
                   (n.since, n.created_at, n.id) > (d.since, d.created_at, d.id) AS legacy_newer
            FROM agent_entity_aliases n
            JOIN agent_projects p ON p.tenant_id = n.tenant_id AND p.is_default
            JOIN agent_entity_aliases d
              ON d.tenant_id = n.tenant_id AND d.project_id = p.id
             AND d.end_user_id IS NOT DISTINCT FROM n.end_user_id
             AND d.agent_id IS NOT DISTINCT FROM n.agent_id
             AND d.alias_lower = n.alias_lower
            WHERE n.project_id IS NULL
        )
        DELETE FROM agent_entity_aliases x
        USING pairs
        WHERE x.id = CASE WHEN pairs.legacy_newer THEN pairs.there ELSE pairs.legacy END
    """)
    op.execute("""
        UPDATE agent_entity_aliases x
        SET project_id = p.id
        FROM agent_projects p
        WHERE p.tenant_id = x.tenant_id AND p.is_default
          AND x.project_id IS NULL
    """)


def downgrade() -> None:
    pass
