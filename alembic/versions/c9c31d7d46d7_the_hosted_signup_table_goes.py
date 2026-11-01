"""the hosted signup table goes

Revision ID: c9c31d7d46d7
Revises: a3c5e7f9b204
Create Date: 2026-09-28

`agent_shadow_accounts` belongs to the hosted service. There, `korely init
--agent` mints an account with no human in the loop, and this table kept the
caller's name and signup IP (for throttling signups per address) and a
`claimed_at` for a later claim by a person. This product has no such route:
an account is made on the box, by whoever operates it (`manage create-tenant`,
see the top of korely_agent/manage.py). The table came across with the
extraction, the first migration created it, and no version of this repository
has ever written a row to it or read one from it.

Rows on an existing install. None were written by this product. An install
holds some only if somebody put them there, by hand or by loading data from
the hosted service, and they are hosted signup records: a caller name, an IP
address (personal data, kept for a purpose this product does not have) and
two dates. The upgrade deletes them with the table, on purpose. To keep them,
take `./deploy/backup.sh` before upgrading, as for any upgrade.

The downgrade recreates the table exactly as the first migration made it:
the same columns, types, default, key, cascade to `tenants` and the three
indexes, under the same names. It comes back empty. What the upgrade deleted
is in the backup, not here.
"""
import sqlalchemy as sa
from alembic import op

revision = "c9c31d7d46d7"
down_revision = "a3c5e7f9b204"
branch_labels = None
depends_on = None

TABLE = "agent_shadow_accounts"
INDEXED = ("created_at", "signup_ip", "tenant_id")


def upgrade() -> None:
    for column in INDEXED:
        op.drop_index(op.f(f"ix_{TABLE}_{column}"), table_name=TABLE)
    op.drop_table(TABLE)


def downgrade() -> None:
    # As in a42dd655f8df, line for line.
    op.create_table(
        TABLE,
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("agent_caller", sa.String(length=120), nullable=True),
        sa.Column("signup_ip", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in INDEXED:
        op.create_index(op.f(f"ix_{TABLE}_{column}"), TABLE, [column], unique=False)
