"""the hosted service's tables

Revision ID: 6e186e7f9cad
Revises: a4e9c2d7b51f
Create Date: 2026-10-08 20:31:32

One codebase for the install somebody runs and the service Korely runs (the
decision of 8 Oct 2026). The service needs to know who signs in to
an account, its plan, what it used this month, the top-ups it paid, the
Polar events already handled, the providers stopped by a billing alert, and
the proof that an erased account was erased. Six tables, created on every
install because there is one chain of migrations; an install of one's own
leaves them empty (korely_agent/hosted/models.py).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "6e186e7f9cad"
down_revision = "a4e9c2d7b51f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hosted_accounts",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("firebase_uid", sa.String(128), nullable=True),
        sa.Column("sign_in_method", sa.String(32), nullable=True),
        sa.Column("plan", sa.String(16), nullable=False, server_default="hobby"),
        sa.Column("is_superadmin", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("born_without_login", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("agent_caller", sa.String(120), nullable=True),
        sa.Column("signup_network", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("plan IN ('hobby', 'developer', 'team', 'scale')",
                           name="ck_hosted_accounts_plan"),
        sa.CheckConstraint("email = lower(email)", name="ck_hosted_accounts_email_lower"),
    )
    op.create_index("uq_hosted_accounts_email", "hosted_accounts", ["email"], unique=True)
    op.create_index("uq_hosted_accounts_firebase_uid", "hosted_accounts", ["firebase_uid"], unique=True)
    op.create_index("idx_hosted_accounts_signups", "hosted_accounts",
                    ["born_without_login", "created_at"])

    op.create_table(
        "hosted_usage_months",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("month", sa.String(6), primary_key=True),
        sa.Column("writes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("queries", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "hosted_topups",
        sa.Column("order_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("month", sa.String(6), nullable=False),
        sa.Column("writes", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_hosted_topups_tenant_month", "hosted_topups", ["tenant_id", "month"])

    op.create_table(
        "hosted_polar_events",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "hosted_brakes",
        sa.Column("provider", sa.String(32), primary_key=True),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("pulled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "hosted_erasures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email_sha256", sa.String(64), nullable=True),
        sa.Column("firebase_uid_sha256", sa.String(64), nullable=True),
        sa.Column("via", sa.String(16), nullable=False),
        sa.Column("rows", sa.JSON(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("via IN ('dashboard', 'api')", name="ck_hosted_erasures_via"),
    )
    op.create_index("idx_hosted_erasures_uid", "hosted_erasures", ["firebase_uid_sha256"])
    op.create_index("idx_hosted_erasures_email", "hosted_erasures", ["email_sha256"])


def downgrade() -> None:
    op.drop_table("hosted_erasures")
    op.drop_table("hosted_brakes")
    op.drop_table("hosted_polar_events")
    op.drop_table("hosted_topups")
    op.drop_table("hosted_usage_months")
    op.drop_table("hosted_accounts")
