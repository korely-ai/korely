"""The sign-ups the caps count, kept apart from the accounts

Revision ID: e7c3a9b5d2f4
Revises: d5b9f1c3a7e2
Create Date: 2026-10-17 22:00:00

The audit of 17 Oct 2026: the caps on accounts made without a login counted
the live rows of hosted_accounts, so adopting such an account or closing it
reopened the cap. A row per sign-up, outside the account, cleared after three
days. The last three days of sign-ups are copied in, so the caps hold across
the upgrade.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e7c3a9b5d2f4"
down_revision = "d5b9f1c3a7e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hosted_signups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("network", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("idx_hosted_signups_created", "hosted_signups", ["created_at"])
    op.create_index("idx_hosted_signups_network", "hosted_signups", ["network", "created_at"])
    op.execute("""
        INSERT INTO hosted_signups (id, network, created_at)
        SELECT tenant_id, signup_network, created_at FROM hosted_accounts
        WHERE born_without_login AND created_at >= now() - interval '3 days'
    """)


def downgrade() -> None:
    op.drop_index("idx_hosted_signups_network", table_name="hosted_signups")
    op.drop_index("idx_hosted_signups_created", table_name="hosted_signups")
    op.drop_table("hosted_signups")
