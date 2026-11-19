"""the install remembers which model computed its vectors

Revision ID: 5d2b8e4f7a13
Revises: 761f3fb02bbb
Create Date: 2026-10-06 15:30:00
"""
import sqlalchemy as sa
from alembic import op

revision = "5d2b8e4f7a13"
down_revision = "761f3fb02bbb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """What the install knows about itself, in clear text. Its first row is
    the model of the stored vectors: the default embedder changed in 0.1.19,
    both models give 768 numbers, and the start compares the model in force
    with this one (services/vectors.py). Empty after the upgrade: the first
    start fills it, once the stored vectors are found to be the model's."""
    op.create_table(
        "install_state",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("install_state")
