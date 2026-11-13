"""the accounts made without a login before the trial existed

Revision ID: b6e2d9f4a8c1
Revises: e4b7c1d9a2f6
Create Date: 2026-11-10 23:00:00

`hosted_accounts.born_before_the_trial`, false by default: an account born
without a login before 1.0.10, when `POST /v1/agents/init` made keys with no
end date. Nobody having adopted it, it is on trial like every account born
there, but its writes stay its plan's a month, as they were when it was
made, and not the trial's in all (hosted/accounts.trial_writes_in_all): from
1.0.10 the trial counted their earlier writes too, and one that had written
more than the trial's writes was refused at its first write after the
upgrade.

Set here once, never by a sign-up, from what the release that made the
account left on its rows: from 1.0.10 the key `init` makes (named
`agent-cli`, the name a sign-up gives it and that only an adoption changes)
has an end date; before, it had none. So an account born without a login,
not adopted (no login), with no `agent-cli` key that has an end date, was
made before 1.0.10. An account whose key has expired or was revoked keeps
its row and its end date, so it stays a trial account. An adopted account
is left false: it is not on trial, and the flag is never read for it. No
key is touched: the keys made before 1.0.10 keep no end date.

Only the Cloud fills `hosted_accounts`; on an install of one's own the
table is empty and the update changes nothing.

The downgrade drops the column.
"""
import sqlalchemy as sa
from alembic import op

revision = "b6e2d9f4a8c1"
down_revision = "e4b7c1d9a2f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosted_accounts", sa.Column(
        "born_before_the_trial", sa.Boolean(), nullable=False, server_default="false"))
    op.execute("""
        UPDATE hosted_accounts AS a SET born_before_the_trial = true
        WHERE a.born_without_login AND coalesce(a.firebase_uid, '') = ''
          AND NOT EXISTS (
              SELECT 1 FROM agent_api_keys AS k
              WHERE k.tenant_id = a.tenant_id AND k.name = 'agent-cli'
                AND k.expires_at IS NOT NULL)
    """)


def downgrade() -> None:
    op.drop_column("hosted_accounts", "born_before_the_trial")
