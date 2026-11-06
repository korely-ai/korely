"""the two tables nothing read or wrote

Revision ID: 2e7a9c4b1d58
Revises: 8c41f2a9d6e3
Create Date: 2026-10-06 17:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = "2e7a9c4b1d58"
down_revision = "8c41f2a9d6e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """`agents` and `agent_daily_usage`, left by the hosted product's quotas:
    no line of this product read or wrote them (the audit of 2026-10-06, row
    14). An install that has rows in them never put them there."""
    op.drop_index("idx_agents_tenant", table_name="agents")
    op.drop_table("agents")
    op.drop_table("agent_daily_usage")


def downgrade() -> None:
    op.create_table(
        "agent_daily_usage",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("ops_count", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "date"),
    )
    op.create_table(
        "agents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_agents_tenant", "agents", ["tenant_id"], unique=False)
