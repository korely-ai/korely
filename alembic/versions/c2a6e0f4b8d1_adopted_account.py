"""The account an adoption replaced

Revision ID: c2a6e0f4b8d1
Revises: b4e8d2a6c1f3
Create Date: 2026-10-17 20:00:00

The audit of 17 Oct 2026: a person with a checkout still open who adopts a
key's account loses the empty account that opened it; when the checkout is
paid, its events named a tenant that no longer exists and were ignored, and
the subscription was charged for nothing. The account that stayed keeps the
id of the one it replaced, and those events apply to it.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c2a6e0f4b8d1"
down_revision = "b4e8d2a6c1f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosted_accounts",
                  sa.Column("adopted_tenant_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("idx_hosted_accounts_adopted", "hosted_accounts", ["adopted_tenant_id"])


def downgrade() -> None:
    op.drop_index("idx_hosted_accounts_adopted", table_name="hosted_accounts")
    op.drop_column("hosted_accounts", "adopted_tenant_id")
