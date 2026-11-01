"""the tokens a provider served from its prompt cache

Revision ID: 7b3e1d9a4c26
Revises: 2e7a9c4b1d58
Create Date: 2026-10-07 18:10:00
"""
import sqlalchemy as sa
from alembic import op

revision = "7b3e1d9a4c26"
down_revision = "2e7a9c4b1d58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """`agent_llm_usage.cached_tokens`: the part of `prompt_tokens` the provider
    served from its prompt cache (OpenAI-compatible
    `usage.prompt_tokens_details.cached_tokens`, Gemini
    `usage_metadata.cached_content_token_count`). 0 for the rows before it and
    for a provider that says nothing: nothing known to be cached."""
    op.add_column("agent_llm_usage",
                  sa.Column("cached_tokens", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("agent_llm_usage", "cached_tokens")
