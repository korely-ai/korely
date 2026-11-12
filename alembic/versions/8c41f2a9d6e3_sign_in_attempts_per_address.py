"""wrong passwords and locks per address, not per operator

Revision ID: 8c41f2a9d6e3
Revises: 5d2b8e4f7a13
Create Date: 2026-10-06 16:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = "8c41f2a9d6e3"
down_revision = "5d2b8e4f7a13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """The lock moves from the operator to the pair address and username: a
    lock on the operator let anybody who knew the name keep the operator out
    (the audit of 2026-10-06, row 7). The two columns go; a lock in force
    when this runs is lost, and the attempts start counting again."""
    op.create_table(
        "login_attempts",
        sa.Column("ip", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("ip", "username"),
    )
    op.drop_column("operators", "failed_attempts")
    op.drop_column("operators", "locked_until")


def downgrade() -> None:
    op.add_column("operators", sa.Column("locked_until", sa.DateTime(timezone=True),
                                         nullable=True))
    op.add_column("operators", sa.Column("failed_attempts", sa.Integer(),
                                         server_default="0", nullable=False))
    op.drop_table("login_attempts")
