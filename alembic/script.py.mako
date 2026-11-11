"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from alembic import op
import sqlalchemy as sa

# Always imported: autogenerate emits pgvector.sqlalchemy.vector.VECTOR(...) for
# embedding columns but does not add the import itself, and a migration that
# raises NameError takes the container down before the application starts. The
# unused-import warning is cheaper than a boot loop.
import pgvector.sqlalchemy  # noqa: F401
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
