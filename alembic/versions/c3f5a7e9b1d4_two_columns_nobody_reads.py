"""two columns nothing reads: tenants.deleted_at, operator_sessions.last_seen_at

Revision ID: c3f5a7e9b1d4
Revises: b8e4d2f6a9c1
Create Date: 2026-10-17 23:30:00

The audit of 17 Oct 2026. `tenants.deleted_at` was never written nor
read: it made a soft delete look possible that nothing honours, since an
account is deleted for real (hosted/closing.py). `operator_sessions.last_seen_at`
was written, with a commit, on every request of the dashboard, and read by
nothing. Both are nullable and carry nothing anybody asked for, so they go.

The downgrade adds them back, empty.
"""
import sqlalchemy as sa
from alembic import op

revision = "c3f5a7e9b1d4"
down_revision = "b8e4d2f6a9c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("tenants", "deleted_at")
    op.drop_column("operator_sessions", "last_seen_at")


def downgrade() -> None:
    op.add_column("operator_sessions",
                  sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tenants", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
