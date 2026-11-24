"""an account without a login that no key reaches is noticed, then erased

Revision ID: d8a4f2c6e1b9
Revises: c4e2a9d7b1f3
Create Date: 2026-11-16 01:30:00

`hosted_accounts.erasure_notice_at`, empty by default: when an account born
without a login, not adopted, was found with no key that works, and its
webhooks were told; `hosted_accounts.erasure_tried_at`, empty too: the last
round whose erasure of it failed, so the others go first next time.
`hosted_erasures.via` may also say `retention`: the
proof of such an account's erasure, made by the same close as the others.
No row is filled in here: the rounds that fill them stamp the accounts
from the day they start, which is not the upgrade, and none is erased
before its stamp is 30 days old.

Only the service fills these tables; on an install of one's own they are
empty and nothing changes.

The downgrade drops the two columns, and files the proofs of `retention` under
`api`, the door the constraint of before allows for an account without a
login: the label is lost, the proof stays.
"""
import sqlalchemy as sa
from alembic import op

revision = "d8a4f2c6e1b9"
down_revision = "c4e2a9d7b1f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosted_accounts", sa.Column(
        "erasure_notice_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("hosted_accounts", sa.Column(
        "erasure_tried_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint("ck_hosted_erasures_via", "hosted_erasures", type_="check")
    op.create_check_constraint("ck_hosted_erasures_via", "hosted_erasures",
                               "via IN ('dashboard', 'api', 'retention')")


def downgrade() -> None:
    op.execute("UPDATE hosted_erasures SET via = 'api' WHERE via = 'retention'")
    op.drop_constraint("ck_hosted_erasures_via", "hosted_erasures", type_="check")
    op.create_check_constraint("ck_hosted_erasures_via", "hosted_erasures",
                               "via IN ('dashboard', 'api')")
    op.drop_column("hosted_accounts", "erasure_tried_at")
    op.drop_column("hosted_accounts", "erasure_notice_at")
