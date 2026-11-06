"""the model key is set from the screen

Revision ID: 99bb3d378f32
Revises: 4565f3e40de9
Create Date: 2026-09-12 11:27:58.558604
"""
from alembic import op
import sqlalchemy as sa

# Always imported: autogenerate emits pgvector.sqlalchemy.vector.VECTOR(...) for
# embedding columns but does not add the import itself, and a migration that
# raises NameError takes the container down before the application starts. The
# unused-import warning is cheaper than a boot loop.
import pgvector.sqlalchemy  # noqa: F401


revision = '99bb3d378f32'
down_revision = '4565f3e40de9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """A place for settings an operator changes from the screen.

    One row today: the model provider key, encrypted with SECRET_KEY before it
    is written. It used to live only in `.env`, which meant that the moment a
    customer bought their own key, somebody had to open an SSH session and
    restart the service.
    """
    op.create_table(
        "install_settings",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("name"),
    )
    # Autogenerate also proposed dropping the two HNSW indexes, because they
    # were created in raw SQL and never declared on the models. They are
    # declared now, so the next comparison sees them on both sides. Dropping
    # them here is how a performance fix disappears three months later without
    # anybody deciding to remove it.


def downgrade() -> None:
    op.drop_table("install_settings")
