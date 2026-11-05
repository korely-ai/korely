"""a key.expiring notice nobody heard is due again

Revision ID: c5e9a3f7b1d4
Revises: b7d2e9f4a1c6
Create Date: 2026-11-03 18:00:00

Rilievi del codice, 1 Nov 2026. Up to 1.0.7 the expiry round stamped
`expiry_warned_at` whenever the enqueue did not fail, and `emit_event`
answers 0, not None, when no endpoint takes the event: a key that entered its
last two weeks before the account had a webhook was marked as warned with
nothing sent. From this release a `key.expiring` is spent only on a queued
delivery (services/keys.warn_about_expiring), but the keys the old round
stamped keep their stamp, and an endpoint added within their two weeks
would still hear only `key.expired`.

Here the stamp of a key that has not expired yet is cleared where no
`key.expiring` delivery about it was ever queued, so the round sends it to
the first endpoint that takes it. A key already expired keeps its stamp: the
round sends it `key.expired`, as for any key told only that it was expiring.
Revoked keys and the keys of an OAuth client are left as they are, as the
round leaves them out.

A delivery is the only record that a notice went. A finished one is deleted
after `WEBHOOK_DELIVERY_RETENTION_DAYS` (30 by default, more than the two
weeks), and every delivery of an endpoint goes with it when it is deleted:
a key whose notice was queued to an endpoint deleted since, or purged under a
retention shorter than two weeks, is told again (the trade-off: a second
notice, against the lost one this repairs). One statement; the deliveries
are probed by their (tenant, created_at) index, once per stamped key.

The downgrade leaves the stamps as they are: which rows were cleared is not
recorded, and the round before this release reads a cleared stamp as a key
never told.
"""
import sqlalchemy as sa
from alembic import op

revision = "c5e9a3f7b1d4"
down_revision = "b7d2e9f4a1c6"
branch_labels = None
depends_on = None


def repair(bind) -> int:
    """Clear the stamp of each unexpired key no `key.expiring` delivery is
    about; the count. Run again, it clears nothing new."""
    return bind.execute(sa.text("""
        UPDATE agent_api_keys k
        SET expiry_warned_at = NULL
        WHERE k.expiry_warned_at IS NOT NULL
          AND k.expires_at > now()
          AND k.revoked_at IS NULL
          AND k.oauth_client_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM agent_webhook_deliveries d
              WHERE d.tenant_id = k.tenant_id AND d.event_type = 'key.expiring'
                AND d.payload -> 'data' ->> 'key_id' = k.id::text)
    """)).rowcount


def upgrade() -> None:
    repair(op.get_bind())


def downgrade() -> None:
    pass
