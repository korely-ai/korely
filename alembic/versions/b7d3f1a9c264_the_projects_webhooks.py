"""the project of a webhook endpoint

Revision ID: b7d3f1a9c264
Revises: 6e186e7f9cad
Create Date: 2026-10-08 22:00:00

The one codebase (8 Oct 2026): an endpoint may belong to one project, and then
it receives that project's events and the account's; NULL (every endpoint of
today) receives every project's, as before. A deleted project takes its
endpoints with it. services/webhooks.py has the rule.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "b7d3f1a9c264"
down_revision = "6e186e7f9cad"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_webhook_endpoints",
                  sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("agent_webhook_endpoints_project_id_fkey", "agent_webhook_endpoints",
                          "agent_projects", ["project_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_agent_webhook_endpoints_project_id", "agent_webhook_endpoints",
                    ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_webhook_endpoints_project_id", table_name="agent_webhook_endpoints")
    op.drop_constraint("agent_webhook_endpoints_project_id_fkey", "agent_webhook_endpoints",
                       type_="foreignkey")
    op.drop_column("agent_webhook_endpoints", "project_id")
