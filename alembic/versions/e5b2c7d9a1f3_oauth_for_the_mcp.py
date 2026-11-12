"""OAuth for the MCP: the clients, the grants, the key a client was issued

Revision ID: e5b2c7d9a1f3
Revises: d8c4e1a7b3f9
Create Date: 2026-10-16 01:20:00

ChatGPT cannot present an API key and Claude.ai prefers not to: both connect
a remote MCP server through OAuth (16 Oct 2026). `oauth_clients` holds the
clients that register themselves (RFC 7591), `oauth_grants` the requests
waiting for consent, the authorization codes and the refresh tokens (hashed),
and `agent_api_keys.oauth_client_id` marks the one key each connected app is
issued. Empty tables and a nullable column: nothing changes for the keys of
before.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e5b2c7d9a1f3"
down_revision = "d8c4e1a7b3f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(64), primary_key=True),
        sa.Column("info", sa.JSON(), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "oauth_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=True, unique=True),
        sa.Column("client_id", sa.String(64),
                  sa.ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("key_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agent_api_keys.id", ondelete="CASCADE"), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("resource", sa.String(512), nullable=True),
        sa.Column("redirect_uri", sa.Text(), nullable=True),
        sa.Column("redirect_uri_explicit", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("code_challenge", sa.String(128), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('request', 'code', 'refresh')", name="ck_oauth_grants_kind"),
    )
    op.create_index("idx_oauth_grants_key", "oauth_grants", ["key_id"])
    op.add_column("agent_api_keys", sa.Column(
        "oauth_client_id", sa.String(64),
        sa.ForeignKey("oauth_clients.client_id", ondelete="SET NULL"), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_api_keys", "oauth_client_id")
    op.drop_index("idx_oauth_grants_key", table_name="oauth_grants")
    op.drop_table("oauth_grants")
    op.drop_table("oauth_clients")
