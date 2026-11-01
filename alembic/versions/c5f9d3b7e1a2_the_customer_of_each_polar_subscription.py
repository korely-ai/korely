"""the Polar customer of each subscription

Revision ID: c5f9d3b7e1a2
Revises: b4e8c2a6d9f1
Create Date: 2026-10-20 11:00:00

The audit of 20 Oct 2026 (the Cloud's billing): the account's Polar lookups
went through the one customer its latest applied event named, so a late live
event of a subscription it had left, on another customer (Polar attaches a
checkout to an existing customer with the same email), took its live
subscription out of sight: the old one's revocation dropped a paying account
to hobby, and the double-purchase guard let a second checkout through. Each
subscription now keeps the customer its events named, and the lookups go
through every one not revoked.

Empty for the subscriptions applied before: they are filled by their next
event, and until then the account is looked up as before (its latest
customer, else its external id).
"""
import sqlalchemy as sa
from alembic import op

revision = "c5f9d3b7e1a2"
down_revision = "b4e8c2a6d9f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosted_polar_subscriptions",
                  sa.Column("customer_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("hosted_polar_subscriptions", "customer_id")
