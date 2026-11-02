"""the example object of a learned relation

Revision ID: a3c5e7f9b204
Revises: f27a0c9e1b01
Create Date: 2026-09-27

The same column as the engine's earlier product had. The MCP acceptance run
of 2026-09-27 found that the vocabulary merged "uses" (Salesforce) with
"subscribes_to" (the plan), because the model saw the relation without its
object. And cardinality depends on the type of the value (Lin, Mausam, Etzioni,
EMNLP 2010 §2: "published in" has one value if it is a year, many if it is a
language): the judge reads "[single-valued for values like: Business plan]" and
compares the type itself. Relations learned earlier stay without an example and
the judge receives the bare label.
"""
import sqlalchemy as sa
from alembic import op

revision = "a3c5e7f9b204"
down_revision = "f27a0c9e1b01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("predicate_clusters", sa.Column("example_object", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("predicate_clusters", "example_object")
