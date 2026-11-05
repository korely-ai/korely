"""Polar subscription events applied in order

Revision ID: b4e8d2a6c1f3
Revises: a7d2c9e4b6f1
Create Date: 2026-10-17 19:30:00

The audit of 17 Oct 2026: the webhook applied subscription events in the
order they arrived, so a late event put a paid plan back after a revocation,
or an older plan back after an upgrade. One row per subscription: Polar's
timestamp of the newest version applied, and when it was revoked.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b4e8d2a6c1f3"
down_revision = "a7d2c9e4b6f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hosted_polar_subscriptions",
        sa.Column("subscription_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("hosted_polar_subscriptions")
