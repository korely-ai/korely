"""Who sets what is the whole install's

Revision ID: d5b9f1c3a7e2
Revises: c2a6e0f4b8d1
Create Date: 2026-10-17 21:00:00

The audit of 17 Oct 2026: on an install with more than one account, the
administrator of any of them (a dashboard login or an admin key) could read,
remove or replace the model provider and its key for every account. The
person allowed to is now marked on the operator; nobody is marked by this
migration, so an install with several accounts names that person with
`manage grant-install-admin --username <name>`. An install with one account
works as before.
"""
import sqlalchemy as sa
from alembic import op

revision = "d5b9f1c3a7e2"
down_revision = "c2a6e0f4b8d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("operators", sa.Column("install_admin", sa.Boolean(), nullable=False,
                                         server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("operators", "install_admin")
