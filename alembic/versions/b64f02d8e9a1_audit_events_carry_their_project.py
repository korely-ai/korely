"""audit events carry the project they were produced in

Revision ID: b64f02d8e9a1
Revises: 5e1d9a7c3b28
Create Date: 2026-09-28

Every read a key can make is behind its project's wall (core/auth.key_project)
except one: GET /v1/audit read `audit_events` by account, so any key listed
the end user ids, memory ids and actions of every project of the account, and
could ask for them by `user_id`. /v1/users and /v1/agents were walled for the
same reason on 12 September; the audit trail had no column to wall it with.

It has one now, stamped when a row is written with the project of the key
that made the call, and GET /v1/audit reads its key's project only. The
operator's screen (/admin/requests) keeps reading the whole account.

Existing rows take the project of the key that produced them: its project, or
the account's default project when the key has none, which is how
`key_project` resolves such a key. That is the key's project today, the best
record there is; a key moved between projects since then files its older rows
under the new one. A row with no key (the MCP recorded none before this) stays
NULL rather than guessed: no key reads it, the operator still does.

The downgrade drops the column and its index; nothing else was changed.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b64f02d8e9a1"
down_revision = "5e1d9a7c3b28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_events",
                  sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_audit_events_project", "audit_events", "agent_projects",
        ["project_id"], ["id"], ondelete="SET NULL",
    )
    op.execute("""
        UPDATE audit_events e
        SET project_id = COALESCE(
            k.project_id,
            (SELECT p.id FROM agent_projects p
             WHERE p.tenant_id = k.tenant_id AND p.is_default
             ORDER BY p.created_at LIMIT 1))
        FROM agent_api_keys k
        WHERE e.api_key_id = k.id
    """)
    op.create_index("idx_audit_tenant_project_ts", "audit_events",
                    ["tenant_id", "project_id", "ts"])


def downgrade() -> None:
    op.drop_index("idx_audit_tenant_project_ts", table_name="audit_events")
    op.drop_constraint("fk_audit_events_project", "audit_events", type_="foreignkey")
    op.drop_column("audit_events", "project_id")
