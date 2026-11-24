"""work sessions apart from notes

Revision ID: e9b3d7a1c5f2
Revises: d6a2f8c4e1b7
Create Date: 2026-10-18 12:30:00

Decided on 18 Oct 2026. The hooks of a coding
assistant save each work session as a memory, and on the session bench of
17 Oct 71% of a session's facts were working artefacts (commands, paths,
quoted values), three of them a person's sentences read as the repository's.
Nothing could leave them out of a read. A memory now says what kind of text
it is (`kind`: NULL a note, "session" a work session), and a fact whether
only sessions stated it (`session_only`), so a read can leave both out.

Every memory written before is a note, and every fact `session_only` false:
nothing can tell an old session from a note, so nothing old is hidden.
"""
import sqlalchemy as sa
from alembic import op

revision = "e9b3d7a1c5f2"
down_revision = "d6a2f8c4e1b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_memories", sa.Column("kind", sa.String(16), nullable=True))
    op.add_column("agent_facts", sa.Column("session_only", sa.Boolean(), nullable=False,
                                           server_default=sa.text("false")))


def downgrade() -> None:
    op.drop_column("agent_facts", "session_only")
    op.drop_column("agent_memories", "kind")
