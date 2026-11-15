"""a Polar subscription put on record is not one applied

Revision ID: d8a4f2c6b1e9
Revises: c5e9a3f7b1d4
Create Date: 2026-11-03 12:00:00

Audit run 6 (korely.hosted.billing.polar-event-order-unchecked): a live
subscription with no row in `hosted_polar_subscriptions` left every lookup
when a newer subscription on another customer moved the account's stored
customer. The rows are now also put on record from Polar's own list
(`billing.on_record`), with their customer and nothing applied. A row was
until now the mark of an applied event: the first event of a subscription
told the founder about a second charge, and the reconciliation's `--fix`
left alone an account that never paid through here (a plan set by hand).
`applied` keeps that mark apart from the row.

Every row made before this revision was made by an applied version
(`billing._in_order`), so all of them are marked applied.

The downgrade drops the column: the rows put on record stay, and the code
before this revision counts them as applied.
"""
import sqlalchemy as sa
from alembic import op

revision = "d8a4f2c6b1e9"
down_revision = "c5e9a3f7b1d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosted_polar_subscriptions",
                  sa.Column("applied", sa.Boolean(), nullable=False, server_default="false"))
    op.execute("UPDATE hosted_polar_subscriptions SET applied = true")


def downgrade() -> None:
    op.drop_column("hosted_polar_subscriptions", "applied")
