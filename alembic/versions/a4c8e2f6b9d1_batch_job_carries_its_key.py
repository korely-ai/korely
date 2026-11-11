"""a batch job carries the key that queued it

Revision ID: a4c8e2f6b9d1
Revises: e9b3d7a1c5f2
Create Date: 2026-10-18 16:00:00

Audit of 18 Oct 2026. A job of POST /v1/batch kept its account and
its project, not its key: once the key was revoked, expired or disconnected
as an OAuth app, every route refused it, and every job it had queued went on
importing, into an archived project or, after a project's deletion, into the
default one. The worker now imports an item only while the key that queued
the job could still write there (services/batch._key_still_writes).

Jobs queued before this have no key to ask (NULL) and are imported as before.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a4c8e2f6b9d1"
down_revision = "e9b3d7a1c5f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_batch_jobs",
                  sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_agent_batch_jobs_api_key", "agent_batch_jobs", "agent_api_keys",
        ["api_key_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_agent_batch_jobs_api_key", "agent_batch_jobs", type_="foreignkey")
    op.drop_column("agent_batch_jobs", "api_key_id")
