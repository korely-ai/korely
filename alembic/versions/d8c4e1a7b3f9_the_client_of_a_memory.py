"""the client of a memory

Revision ID: d8c4e1a7b3f9
Revises: c3a7d9e2f415
Create Date: 2026-10-15 19:30:00

Which tool wrote a memory (services/clients.py): the X-Korely-Client header,
or the name an MCP client gives when it connects. A label outside the agent
cap, for a developer with several coding assistants on one memory (the open
memory analysis of 15 Oct 2026). NULL for every memory written before, and
for a write that names no client.
"""
import sqlalchemy as sa
from alembic import op

revision = "d8c4e1a7b3f9"
down_revision = "c3a7d9e2f415"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_memories", sa.Column("client", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_memories", "client")
