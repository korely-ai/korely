"""the delivery queue is emptied, and read through an index

Revision ID: e7b21c4a9f30
Revises: d3f8a1c05b92

Nothing cleaned `agent_webhook_deliveries`. Audit, model usage, operator
sessions and counters were all in the purge job, and this table was not, so it
grew for ever.

Found by following a tester's criterion for upgrades, not by looking for it:
it is the kind of growth nobody notices at the time, because it only shows
when a page that reads the table gets slow.

Measured first: on 200,000 deliveries, the webhook list sorted ALL of the
tenant's rows to take 500. Fourteen milliseconds, so not urgent, but a full
sort that grows with the table.

The index serves the only two questions that table is asked: "the most recent
for this tenant" for the list, and "the oldest" for the purge.
"""
from alembic import op

revision = 'e7b21c4a9f30'
down_revision = 'd3f8a1c05b92'
branch_labels = None
depends_on = None

INDEX = "idx_agent_webhook_deliveries_tenant_recent"


def upgrade() -> None:
    op.create_index(INDEX, "agent_webhook_deliveries", ["tenant_id", "created_at"])


def downgrade() -> None:
    op.drop_index(INDEX, table_name="agent_webhook_deliveries")
