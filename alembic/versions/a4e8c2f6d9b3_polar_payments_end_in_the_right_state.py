"""polar payments end in the right state

Revision ID: a4e8c2f6d9b3
Revises: c3f7a9e5b1d8
Create Date: 2026-10-28 14:00:00

The review of the Cloud's Polar billing of 28 Oct 2026.

- `hosted_accounts.polar_checkout_at`: when the account last opened a Polar
  checkout, written before Polar is asked. A claim never deletes an account
  with one (hosted/claim.is_empty): an account that paid and claimed a key
  before the payment's webhook was applied was deleted, and its Polar
  customer with it, which ends the paid subscription. NULL for every
  account already there: the claim then asks Polar.
- `hosted_topups.refunded_writes`: the writes a refund took back, the share
  refunded (billing.file_topup). A partial refund took back the whole
  top-up. The top-ups already refunded took all of theirs back, and say so.
- `hosted_polar_unfiled`: the Polar events our checkout made of a product no
  setting names, kept until `cli refile` files them (billing.Unfiled);
  they were answered 2xx and lost.

The downgrade drops the table and both columns.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a4e8c2f6d9b3"
down_revision = "c3f7a9e5b1d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosted_accounts", sa.Column("polar_checkout_at", sa.DateTime(timezone=True),
                                               nullable=True))
    op.add_column("hosted_topups", sa.Column("refunded_writes", sa.Integer(), nullable=False,
                                             server_default="0"))
    op.execute("UPDATE hosted_topups SET refunded_writes = writes WHERE refunded_at IS NOT NULL")
    op.create_table(
        "hosted_polar_unfiled",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.String(64), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("idx_hosted_polar_unfiled_received", "hosted_polar_unfiled", ["received_at"])
    op.create_index("idx_hosted_polar_unfiled_tenant", "hosted_polar_unfiled", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("idx_hosted_polar_unfiled_tenant", table_name="hosted_polar_unfiled")
    op.drop_index("idx_hosted_polar_unfiled_received", table_name="hosted_polar_unfiled")
    op.drop_table("hosted_polar_unfiled")
    op.drop_column("hosted_topups", "refunded_writes")
    op.drop_column("hosted_accounts", "polar_checkout_at")
