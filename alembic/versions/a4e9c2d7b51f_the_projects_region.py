"""the region of a project: where its memories are read by a model

Revision ID: a4e9c2d7b51f
Revises: 8d4f1a2c6e35
Create Date: 2026-10-08 21:30:00

The one codebase (8 Oct 2026): the hosted service runs this code, and there
each project chooses its region, "eu" (Scaleway in Paris) or "global"
(Gemini). services/regions.py has the rules. NULL follows the install's
default writer: every existing project, so an install that never chooses
keeps one writer for all, as before. Nothing to backfill; the downgrade
drops the column.
"""
import sqlalchemy as sa
from alembic import op

revision = "a4e9c2d7b51f"
down_revision = "8d4f1a2c6e35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_projects", sa.Column("region", sa.String(length=16), nullable=True))
    op.create_check_constraint("ck_agent_projects_region", "agent_projects",
                               "region IS NULL OR region IN ('eu', 'global')")


def downgrade() -> None:
    op.drop_constraint("ck_agent_projects_region", "agent_projects", type_="check")
    op.drop_column("agent_projects", "region")
