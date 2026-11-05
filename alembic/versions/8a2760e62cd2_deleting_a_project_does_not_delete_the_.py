"""deleting a project does not delete its memories

Revision ID: 8a2760e62cd2
Revises: 66f560f24d4d
Create Date: 2026-09-12 08:11:12.374271
"""
from alembic import op

# Always imported: autogenerate emits pgvector.sqlalchemy.vector.VECTOR(...) for
# embedding columns but does not add the import itself, and a migration that
# raises NameError takes the container down before the application starts. The
# unused-import warning is cheaper than a boot loop.
import pgvector.sqlalchemy  # noqa: F401


revision = '8a2760e62cd2'
down_revision = '66f560f24d4d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CASCADE meant that deleting a project deleted the memories and facts that
    # carried its id. Nothing writes those columns yet, so the trap has never
    # fired; it would have fired the first time L2 set them and somebody used
    # the delete button the dashboard is about to grow.
    #
    # The constraints are named rather than left to the server, so downgrade can
    # find them again. Autogenerate emitted `drop_constraint(None, ...)` here,
    # which raises rather than reverting.
    for table in ("agent_facts", "agent_memories"):
        op.drop_constraint(f"{table}_project_id_fkey", table, type_="foreignkey")
        op.create_foreign_key(f"{table}_project_id_fkey", table, "agent_projects",
                              ["project_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    for table in ("agent_facts", "agent_memories"):
        op.drop_constraint(f"{table}_project_id_fkey", table, type_="foreignkey")
        op.create_foreign_key(f"{table}_project_id_fkey", table, "agent_projects",
                              ["project_id"], ["id"], ondelete="CASCADE")
