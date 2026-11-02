"""who answered a model call, and where

Revision ID: c2e8a4f6d913
Revises: b7d3f1a9c264
Create Date: 2026-10-08 22:10:00

The one codebase (8 Oct 2026). A cost row's `provider` is the client that
made the call, and every OpenAI-compatible host (Scaleway, OpenRouter,
Mistral...) goes through the same one: a cap on one vendor's monthly spend
(the hosted service's European cap, Scaleway only) could not tell them
apart. `vendor` and `region` are the writer's, written by core/llm.py.
NULL on the rows before, and on the vectors. Nothing to backfill.
"""
import sqlalchemy as sa
from alembic import op

revision = "c2e8a4f6d913"
down_revision = "b7d3f1a9c264"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_llm_usage", sa.Column("vendor", sa.String(length=24), nullable=True))
    op.add_column("agent_llm_usage", sa.Column("region", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_llm_usage", "region")
    op.drop_column("agent_llm_usage", "vendor")
