"""a pseudonym in what the past erasures left

Revision ID: a5d81f3c7e64
Revises: e3a9c5d1f7b2
Create Date: 2026-10-11 21:00:00

From 0.1.27 erasing an end user replaces their id, in the rows the erasure
keeps, with a keyed pseudonym (korely_agent/services/pseudonyms.py, which has
why: a legal review of 11 Oct 2026). This does the
same, once, for the erasures made before it: for every erasure record of an
end user (scope 'user') that still holds an id, the rows of that end user
written up to the erasure, within its project:

  - the erasure records and the access trail (`audit_events`) take the
    pseudonym; the trail's row of the erasure request itself is written just
    after the erasure, so the trail is read up to a minute past it;
  - the cost rows lose the end user, in every project;
  - the webhooks already delivered take the pseudonym in `data.user_id`.

The pseudonym is the one the code computes: HMAC-SHA256 of `tenant:end user`
under the key HKDF derives from SECRET_KEY with the label below (a test holds
the two equal). Without SECRET_KEY the id goes and nothing replaces it, as
the code does. A project erased before this moved its trail to the default
project, and nothing says which end users it held: those rows stay as they
are.

The downgrade changes nothing back: an id replaced is not stored anywhere.
"""
import hashlib
import hmac
import os

import sqlalchemy as sa
from alembic import op

revision = "a5d81f3c7e64"
down_revision = "e3a9c5d1f7b2"
branch_labels = None
depends_on = None

# korely_agent/services/pseudonyms.py: _INFO and PREFIX.
_INFO = b"korely-erasure-pseudonym-v1"
_PREFIX = "erased:"
# The erasure request's own row in the trail follows the erasure's commit.
_AFTER = "60 seconds"


def _key():
    # The key the application reads, from the environment or ./.env
    # (config.get_settings): read from os.environ alone, a migration run by
    # hand from a checkout found none and wrote NULL in place of every past
    # erasure's id, for good (audit of 17 Oct 2026).
    secret = (os.environ.get("SECRET_KEY") or "").strip()
    if not secret:
        try:
            from korely_agent.config import get_settings

            secret = (get_settings().secret_key or "").strip()
        except Exception:  # noqa: BLE001 - settings that do not load: no key
            secret = ""
    if not secret:
        return None
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=_INFO).derive(secret.encode("utf-8"))


def pseudonym(key, tenant_id, end_user_id):
    if key is None or not end_user_id:
        return None
    mac = hmac.new(key, f"{tenant_id}:{end_user_id}".encode("utf-8"), hashlib.sha256)
    return _PREFIX + mac.hexdigest()[:32]


def upgrade() -> None:
    bind = op.get_bind()
    key = _key()
    erasures = bind.execute(sa.text(
        "SELECT tenant_id, end_user_id, project_id, created_at FROM agent_forget_audit"
        " WHERE scope = 'user' AND end_user_id IS NOT NULL AND end_user_id NOT LIKE 'erased:%'"
        " ORDER BY created_at")).fetchall()
    for tenant_id, end_user, project_id, at in erasures:
        p = pseudonym(key, tenant_id, end_user)
        args = {"t": tenant_id, "u": end_user, "p": p, "proj": project_id, "at": at,
                "proj_s": str(project_id) if project_id else None}
        within = "project_id IS NULL" if project_id is None else "project_id = :proj"
        bind.execute(sa.text(
            "UPDATE agent_forget_audit SET end_user_id = :p WHERE tenant_id = :t"
            f" AND end_user_id = :u AND {within} AND created_at <= :at"), args)
        bind.execute(sa.text(
            "UPDATE audit_events SET end_user_id = :p WHERE tenant_id = :t"
            f" AND end_user_id = :u AND {within}"
            f" AND ts <= CAST(:at AS timestamptz) + interval '{_AFTER}'"), args)
        bind.execute(sa.text(
            "UPDATE agent_llm_usage SET end_user_id = NULL WHERE tenant_id = :t"
            " AND end_user_id = :u AND ts <= :at"), args)
        bind.execute(sa.text(
            "UPDATE agent_webhook_deliveries"
            # jsonb_set of a NULL value is NULL: the payload would go with it.
            " SET payload = jsonb_set(payload::jsonb, '{data,user_id}',"
            " COALESCE(to_jsonb(CAST(:p AS text)), 'null'::jsonb))::json"
            " WHERE tenant_id = :t AND status = 'delivered' AND created_at <= :at"
            " AND payload->'data'->>'user_id' = :u"
            " AND (payload->>'project_id') IS NOT DISTINCT FROM :proj_s"), args)


def downgrade() -> None:
    pass
