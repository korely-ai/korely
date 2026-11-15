"""the Polar customer of an account

Revision ID: c3a7d9e2f415
Revises: b9e4f2c6d813
Create Date: 2026-10-13 15:00:00

A sandbox test of 13 Oct 2026: Polar attached a
checkout to an OLD customer with the same email and another external id, so
every lookup of `GET /v1/customers/external/{tenant}` answered 404: the
double-purchase guard, the portal, the plan change and the closing of the
account all read "never paid". The webhook already found the account from
the checkout's metadata; from now on it also stores the Polar customer it
names, and the lookups go through it. Hosted only; NULL on an install of
one's own and on an account that never paid.
"""
import sqlalchemy as sa
from alembic import op

revision = "c3a7d9e2f415"
down_revision = "b9e4f2c6d813"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosted_accounts", sa.Column("polar_customer_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("hosted_accounts", "polar_customer_id")
