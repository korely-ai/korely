"""the erasure records keep their pseudonym key, and the September ones their project

Revision ID: d8b4e1a7c3f5
Revises: a9d3f6c2e8b4
Create Date: 2026-10-20 12:00:00

The audit of 20 Oct 2026, two steps on `agent_forget_audit`.

1. The September erasures take their project.
   Up to 0.1.11 the end-user erasure wrote its record with no project
   (ef44feb filled it from 0.1.12), while the trail rows of that end user
   carried the key that made them, and b64f02d8e9a1 filed them under the
   key's project. a5d81f3c7e64 then read the record's NULL as "rows of no
   project" and left every one of those trail rows with the erased id in
   clear. The erased id is recovered here from the trail itself: the
   erasure request's own row (action 'erase', with a project and the id in
   clear) written in the minute after the record, whose pseudonym is the
   record's. That end user's rows in that project, up to the erasure,
   take the pseudonym as a5d81f3c7e64 would have given them, and the
   record its project (`erased_in_project_id`), where a restore repeats
   it. A record no such row matches (an account-wide erasure, whose row
   carries the pseudonym, or no SECRET_KEY) stays as it is.

2. Every record that names its end user by pseudonym gets `key_check`,
   the check of the pseudonym key (services/pseudonyms.key_check), so a
   restore under another key refuses instead of finding nobody. The
   records made before this take the check of the key in force
   at the upgrade, the only one known: keep SECRET_KEY as it is across it.
   Without SECRET_KEY they keep none, and an erasures file made of them
   without a key is refused at its export (services/erasure_replay.py).

The downgrade drops the column; the pseudonyms stay, as every erasure's do.
"""
import hashlib
import hmac
import os

import sqlalchemy as sa
from alembic import op

revision = "d8b4e1a7c3f5"
down_revision = "a9d3f6c2e8b4"
branch_labels = None
depends_on = None

# korely_agent/services/pseudonyms.py: _INFO, PREFIX and _CHECKED.
_INFO = b"korely-erasure-pseudonym-v1"
_PREFIX = "erased:"
_CHECKED = b"korely-pseudonym-key-check"
# The erasure request's own row in the trail follows the erasure's commit.
_AFTER = "60 seconds"


def _key():
    # As a5d81f3c7e64: the environment, then ./.env through the settings.
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


def key_check(key):
    return None if key is None else hmac.new(key, _CHECKED, hashlib.sha256).hexdigest()[:32]


def _september(bind, key) -> None:
    records = bind.execute(sa.text(
        "SELECT id, tenant_id, end_user_id, created_at FROM agent_forget_audit"
        " WHERE scope = 'user' AND project_id IS NULL AND erased_in_project_id IS NULL"
        " AND target_id IS NULL AND end_user_id LIKE 'erased:%' ORDER BY created_at")).fetchall()
    for rid, tenant_id, p, at in records:
        rows = bind.execute(sa.text(
            "SELECT DISTINCT end_user_id, project_id FROM audit_events"
            " WHERE tenant_id = :t AND action = 'erase' AND project_id IS NOT NULL"
            " AND end_user_id IS NOT NULL AND end_user_id NOT LIKE 'erased:%'"
            f" AND ts >= :at AND ts <= CAST(:at AS timestamptz) + interval '{_AFTER}'"),
            {"t": tenant_id, "at": at}).fetchall()
        found = [(u, proj) for u, proj in rows if pseudonym(key, tenant_id, u) == p]
        if len(found) != 1:
            continue
        end_user, project_id = found[0]
        args = {"t": tenant_id, "u": end_user, "p": p, "proj": project_id, "at": at,
                "proj_s": str(project_id), "id": rid}
        bind.execute(sa.text(
            "UPDATE audit_events SET end_user_id = :p WHERE tenant_id = :t AND end_user_id = :u"
            f" AND project_id = :proj AND ts <= CAST(:at AS timestamptz) + interval '{_AFTER}'"),
            args)
        bind.execute(sa.text(
            "UPDATE agent_forget_audit SET end_user_id = :p WHERE tenant_id = :t"
            " AND end_user_id = :u AND project_id = :proj AND created_at <= :at"), args)
        bind.execute(sa.text(
            "UPDATE agent_webhook_deliveries"
            " SET payload = jsonb_set(payload::jsonb, '{data,user_id}', to_jsonb(CAST(:p AS text)))::json"
            " WHERE tenant_id = :t AND status = 'delivered' AND created_at <= :at"
            " AND payload->'data'->>'user_id' = :u AND payload->>'project_id' = :proj_s"), args)
        bind.execute(sa.text(
            "UPDATE agent_forget_audit SET erased_in_project_id = :proj WHERE id = :id"), args)


def upgrade() -> None:
    bind = op.get_bind()
    key = _key()
    if key is not None:
        _september(bind, key)
    op.add_column("agent_forget_audit", sa.Column("key_check", sa.String(32), nullable=True))
    check = key_check(key)
    if check is not None:
        bind.execute(sa.text(
            "UPDATE agent_forget_audit SET key_check = :c WHERE end_user_id LIKE 'erased:%'"),
            {"c": check})


def downgrade() -> None:
    op.drop_column("agent_forget_audit", "key_check")
