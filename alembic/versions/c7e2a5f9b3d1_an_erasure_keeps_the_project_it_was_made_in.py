"""an erasure record keeps the project it was made in

Revision ID: c7e2a5f9b3d1
Revises: f2c8a4d6e1b3
Create Date: 2026-10-19 15:00:00

The audit of 19 Oct 2026: deleting a project without `erase`
(services/projects.delete_project) files every row it held under the default
project, its erasure records too, and `agent_forget_audit.project_id` was
the only place that said which project an erasure reached. After a restore
from a dump older than the erasure, the replay (services/erasure_replay.py)
looked for the erased end user or agent namespace in the default project:
it found nobody, and the person came back in clear in the restored project,
or it erased a different end user who shares the id there.

`erased_in_project_id` keeps the project the record was written in, with no
foreign key, so neither a move nor a project's deletion changes it. Filled
from `project_id` for the rows already there: right for every record no
project deletion has moved; one that a deletion moved before this upgrade
says the default project, because where it came from is not recorded
anywhere (the CHANGELOG says what to do about it).

The downgrade drops the column and what it held.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "c7e2a5f9b3d1"
down_revision = "f2c8a4d6e1b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_forget_audit",
                  sa.Column("erased_in_project_id", UUID(as_uuid=True), nullable=True))
    op.execute("UPDATE agent_forget_audit SET erased_in_project_id = project_id")


def downgrade() -> None:
    op.drop_column("agent_forget_audit", "erased_in_project_id")
