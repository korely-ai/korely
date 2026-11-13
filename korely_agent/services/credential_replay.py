"""The credential changes made after a dump, carried across its restore
(deploy/restore.sh), as services/erasure_replay.py carries the erasures.

A restore put back the credentials as the dump had them (audit of 17 Oct
2026, S1): a key revoked since worked again with its scopes, a password reset
since let the old one in again, a session ended since resolved again for up
to 12 hours, a webhook secret rotated since signed again with the old one,
and the OAuth codes and refresh tokens already spent were unspent, so their
replay no longer revoked anything. Revoking is what the README tells an
operator to do with a key found where it should not be, and the restore is
the way back from a failed upgrade, so the restore undid the remedy.

`export` reads, before the restore, the state of every credential of the
live database; `replay`, after it, makes again what changed since the dump,
on the rows both databases hold, always towards the closed side. A key
revoked in the live database is revoked; a key moved there to another
project is moved there again (moving is how an operator narrows a key
without revoking it, and the restore put it back in the project it had
been taken out of: audit of 18 Oct 2026), or revoked when that project
is not in the restored database; an operator disabled there is
disabled, and one who no longer sets the install's models no longer does; a
webhook endpoint switched off there, or deleted from an account the live
database still has, is off. The password and the webhook secret are the ones
in force just before the restore: the person knew that password a minute
ago, and the receiver holds that secret. Nothing is reopened: a key the live
database never revoked is not unrevoked by this.

And whatever the live database said, every operator session, every OAuth
request, code and refresh token, and every key an app got over OAuth end:
that costs a new sign-in and a new consent, and it is the only way to be
sure that nothing ended after the dump is alive again.

The file holds password hashes (scrypt) and webhook secrets in clear, the
same things the dump holds: written owner-only by restore.sh and deleted
after the replay.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from ..db.models import (AgentApiKey, AgentProject, AgentWebhookEndpoint, OAuthGrant,
                         Operator, OperatorSession, Tenant)
from . import audit
from .keys import state_of as key_state


def _when(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _parse(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def export(db: Session) -> dict:
    """The state of every credential of the database, as a JSON-ready dict."""
    return {
        "accounts": [str(t) for (t,) in db.query(Tenant.id).all()],
        "keys": [{"id": str(k.id), "account": str(k.tenant_id), "revoked_at": _when(k.revoked_at),
                  "project": str(k.project_id) if k.project_id else None}
                 for k in db.query(AgentApiKey).all()],
        "operators": [{"id": str(o.id), "account": str(o.tenant_id),
                       "disabled_at": _when(o.disabled_at), "password_hash": o.password_hash,
                       "install_admin": bool(o.install_admin)}
                      for o in db.query(Operator).all()],
        "webhooks": [{"id": str(w.id), "account": str(w.tenant_id), "secret": w.secret,
                      "active": bool(w.active)}
                     for w in db.query(AgentWebhookEndpoint).all()],
    }




def replay(db: Session, live: dict) -> dict:
    """Make again, on the restored database, the credential changes the live
    one held (`live` is what `export` wrote; `{}` when it could not be read),
    and end every session and OAuth grant. Returns the counts."""
    now = datetime.now(timezone.utc)
    accounts = set(live.get("accounts") or [])
    keys = {r["id"]: r for r in live.get("keys") or []}
    operators = {r["id"]: r for r in live.get("operators") or []}
    webhooks = {r["id"]: r for r in live.get("webhooks") or []}
    projects = {str(p) for (p,) in db.query(AgentProject.id).all()}
    out = {"keys_revoked": 0, "keys_moved": 0, "oauth_keys_revoked": 0, "operators_disabled": 0,
           "passwords": 0, "install_admin_removed": 0, "webhook_secrets": 0,
           "webhooks_off": 0, "sessions_ended": 0, "oauth_grants_ended": 0}
    trail = []

    def _record(tenant_id, action: str, target_id: str, **meta) -> None:
        trail.append((tenant_id, action, target_id, meta))

    for k in db.query(AgentApiKey).filter(AgentApiKey.revoked_at.is_(None)).all():
        was = keys.get(str(k.id))
        if k.oauth_client_id is not None:
            # Its secret rotates at every refresh: the dump's may be one the
            # app has long replaced. The app connects again.
            k.revoked_at = now
            out["oauth_keys_revoked"] += 1
            _record(k.tenant_id, audit.KEY_REVOKE, str(k.id), reason="restore: connect again")
        elif was is not None and was.get("revoked_at"):
            k.revoked_at = _parse(was["revoked_at"])
            out["keys_revoked"] += 1
            _record(k.tenant_id, audit.KEY_REVOKE, str(k.id), reason="restore: revoked after the dump")
        elif was is not None and "project" in was and was["project"] != (
                str(k.project_id) if k.project_id else None):
            # A file of an older credentials-export has no "project": the key
            # stays where the dump has it, as before.
            if was["project"] is None or was["project"] in projects:
                k.project_id = UUID(was["project"]) if was["project"] else None
                out["keys_moved"] += 1
                _record(k.tenant_id, audit.KEY_CHANGE, str(k.id), op="move",
                        to_project=was["project"], reason="restore: moved after the dump")
            else:
                # Moved into a project made after the dump: the project the
                # dump gives it is the one it was taken out of.
                k.revoked_at = now
                out["keys_revoked"] += 1
                _record(k.tenant_id, audit.KEY_REVOKE, str(k.id),
                        reason="restore: moved after the dump into a project the dump does not hold")

    for o in db.query(Operator).all():
        was = operators.get(str(o.id))
        if was is None:
            continue
        changed = {}
        if was.get("disabled_at") and o.disabled_at is None:
            o.disabled_at = _parse(was["disabled_at"])
            out["operators_disabled"] += 1
            changed["disabled"] = True
        if was.get("password_hash") and was["password_hash"] != o.password_hash:
            o.password_hash = was["password_hash"]
            out["passwords"] += 1
            changed["password"] = True
        if not was.get("install_admin", True) and o.install_admin:
            o.install_admin = False
            out["install_admin_removed"] += 1
            changed["install_admin"] = False
        if changed:
            _record(o.tenant_id, audit.SETTING_CHANGE, o.username, setting="operator", **changed)

    for w in db.query(AgentWebhookEndpoint).all():
        was = webhooks.get(str(w.id))
        changed = {}
        if was is None:
            # Deleted after the dump from an account the live database still
            # has: off, not deleted, so its owner can look and switch it on.
            if str(w.tenant_id) in accounts and w.active:
                w.active = False
                out["webhooks_off"] += 1
                changed["active"] = False
        else:
            if was.get("secret") and was["secret"] != w.secret:
                w.secret = was["secret"]
                out["webhook_secrets"] += 1
                changed["secret"] = "as before the restore"
            if not was.get("active", True) and w.active:
                w.active = False
                out["webhooks_off"] += 1
                changed["active"] = False
        if changed:
            _record(w.tenant_id, audit.SETTING_CHANGE, str(w.id), setting="webhook", **changed)

    out["sessions_ended"] = int(db.query(OperatorSession).delete(synchronize_session=False) or 0)
    out["oauth_grants_ended"] = int(db.query(OAuthGrant).delete(synchronize_session=False) or 0)
    out["accounts_not_live"] = sum(1 for (t,) in db.query(Tenant.id).all() if str(t) not in accounts)
    db.commit()
    # Each change in the audit trail, as `manage` records its own, once it is
    # made: the live database's record of the first time went with it.
    for tenant_id, action, target_id, meta in trail:
        audit.record(db, tenant_id, action, actor="manage", project_id=None,
                     target_id=target_id, meta={"op": "restore", **meta})
    return out


def in_force(db: Session) -> List[dict]:
    """What can sign in after the restore, per account: the keys that work,
    with the project each reads and writes, the webhook endpoints that are
    on, the operators who are not disabled, with their dates. Printed by `manage credentials-replay` for the person
    running the restore to check against what they expect."""
    now = datetime.now(timezone.utc)
    out = []
    for t in db.query(Tenant).order_by(Tenant.created_at).all():
        # The keys' own state (keys.state_of), so a key every door refuses
        # (a `kor_live_` one on an install of one's own) is not listed in
        # force (audit of 19 Oct 2026).
        keys = [k for k in (db.query(AgentApiKey).filter(AgentApiKey.tenant_id == t.id,
                                                         AgentApiKey.revoked_at.is_(None))
                              .order_by(AgentApiKey.created_at).all())
                if key_state(k, now) == "live"]
        hooks = (db.query(AgentWebhookEndpoint)
                   .filter(AgentWebhookEndpoint.tenant_id == t.id, AgentWebhookEndpoint.active.is_(True))
                   .order_by(AgentWebhookEndpoint.created_at).all())
        ops = (db.query(Operator).filter(Operator.tenant_id == t.id, Operator.disabled_at.is_(None))
                 .order_by(Operator.created_at).all())
        projects = {p.id: p.name for p in db.query(AgentProject).filter(AgentProject.tenant_id == t.id)}
        out.append({"account": t, "keys": keys, "webhooks": hooks, "operators": ops,
                    "projects": projects})
    return out


def dumps(state: Dict) -> str:
    return json.dumps(state, ensure_ascii=False)
