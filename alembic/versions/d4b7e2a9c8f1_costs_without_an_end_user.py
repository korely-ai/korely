"""the cost rows name no end user

Revision ID: d4b7e2a9c8f1
Revises: e7d1b3f9a2c6
Create Date: 2026-10-19 02:00:00

The audit of 19 Oct 2026: `agent_llm_usage` named the end user whose
memory or request made each model call, and an end user's erasure took the
id off those rows inside its own transaction. A cost row written by another
session while that transaction was open read no erasure on file yet, and
kept the erased id in clear for LLM_USAGE_RETENTION_DAYS (400). Nothing read
the column but the erasure itself and the restore replay's search for end
users: the reports, the Cloud's caps and `manage` group by stage, agent and
model, since a cost does not need whose. The column goes, with whatever it
held.

The downgrade gives the column back, empty: the ids it held are not kept
anywhere to put back.
"""
import sqlalchemy as sa
from alembic import op

revision = "d4b7e2a9c8f1"
down_revision = "e7d1b3f9a2c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("agent_llm_usage", "end_user_id")


def downgrade() -> None:
    op.add_column("agent_llm_usage",
                  sa.Column("end_user_id", sa.String(length=255), nullable=True))
