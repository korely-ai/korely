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
on the rows both databases hold. What ends stays ended: a key
revoked in the live database is revoked; a key rotated there has the secret
it had there, the old one ending as the rotation ended it (a rotation
changes the hash on the same row, so the restore had brought the old secret
back and refused the new one: 17 Nov 2026), and the key may do no more than
it could there (a scope taken from it, as the Cloud's adoption takes
`admin`, stays taken; none is added); a key moved there to another
project is moved there again (moving is how an operator narrows a key
without revoking it, and the restore put it back in the project it had
been taken out of: audit of 18 Oct 2026), or revoked when that project
is not in the restored database; an operator disabled there is
disabled, and one who no longer sets the install's models no longer does; a
webhook endpoint switched off there, or deleted from an account the live
database still has, is off. The password, the webhook secret, a key's
secret and project, and (on the Cloud) an account's login are the ones in
force when the file was written: the person knew that password a minute
ago, and the receiver holds that secret. So the file must be the newest
state, never an older one: a file kept from an earlier restore, replayed
onto data that changed after it, put back a secret rotated away since (the
skeptic's S1 of 17 Nov 2026: a key rotated after a leak). The file says
when it was written (`exported_at`, the database's clock), and the replay
refuses, before it changes anything, a file older than the newest moment
the data holds (`newest_moment`). Nothing is reopened: a key the live
database never revoked is not unrevoked by this.

What is authenticated, and what is not. On an install with a SECRET_KEY
the file is signed with a key derived from it (pseudonyms.derived_key,
HMAC-SHA256 over the whole file, its moment included), and the replay
refuses a file this install's SECRET_KEY, or a key in SECRET_KEY_FALLBACKS,
did not sign: whoever writes the file decides passwords, keys and logins.
An unsigned file is refused there whatever it looks like (the skeptic's
round 3 of 17 Nov 2026: a file cut to the shape of 1.0.31's set a chosen
password, and an older one cut so brought an old password back). The one
way past is explicit: `credentials-replay --unsigned-from-1.0.31`, for a
file 1.0.31 wrote (it signed none) for the restore at hand, which is then
read with neither its age nor its author checked, and with nothing in it
that only a later version writes. On an install with no SECRET_KEY nothing
can sign: an unsigned file is read with a warning, its age checked when it
says it. `{}`, what restore.sh hands over when the live database could
not be read, holds nothing to make again and is read as such.

The moments the age is checked against are written by the database's own
clock, as `exported_at` is: a key's rotation (keys.reissue_secret) and an
operator's login (operators.open_session) by core/times.database_now
(`clock_timestamp()`), the other moments by their columns' defaults. The
API's clock wrote the rotation until round 3, and a rotation it stamped
5 s behind the database's was taken for one older than the file.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import (AgentApiKey, AgentProject, AgentWebhookEndpoint, OAuthGrant,
                         Operator, OperatorSession, Tenant)
from ..core import extensions
from ..core.times import database_now
from . import audit, pseudonyms
from .keys import state_of as key_state


def _when(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def moment(value: Optional[str]) -> Optional[datetime]:
    """A moment of the file `export` wrote (this code's own isoformat), or
    None; the Cloud's sections of the file are read with it too."""
    return datetime.fromisoformat(value) if value else None


# The label of the key that signs the file (pseudonyms.derived_key): a new
# scheme gets a new label.
_SIGNS = b"korely-credentials-file-v1"
# What the file of 1.0.31 held, the one version that signed none.
_OF_1_0_31 = {"accounts", "keys", "operators", "webhooks"}
_KEY_OF_1_0_31 = {"id", "account", "revoked_at", "project"}


class Refused(ValueError):
    """The file is not replayed here, and nothing changed: `code` is the
    command's exit status (2: older than the data; 3: not signed here)."""

    def __init__(self, code: int, why: str):
        super().__init__(why)
        self.code = code


def _signature(body: dict, key: bytes) -> str:
    whole = json.dumps({k: v for k, v in body.items() if k != "signature"}, sort_keys=True,
                       separators=(",", ":"), ensure_ascii=False)
    return hmac.new(key, whole.encode("utf-8"), hashlib.sha256).hexdigest()


def signed(body: dict) -> dict:
    """`body` with its signature, under SECRET_KEY; unsigned without one."""
    key = pseudonyms.derived_key(_SIGNS)
    return {**body, "signature": _signature(body, key)} if key else body


def newest_moment(db: Session) -> Optional[datetime]:
    """The newest moment the data holds of its credentials: a key made or
    rotated, an operator made or logged in (a password is changed from a
    login), a webhook endpoint made or changed, an account made. Each only
    moves forward, and the replay writes none past the file's own moment (a
    webhook it changes is one the file had, so a second replay of the same
    file finds it newer, and stops: there is nothing left to make). A key's
    use is not a change of it, and a password reset with no login after the
    file (`manage reset-password`) is not among them: no column holds when
    a password changed."""
    columns = (AgentApiKey.created_at, AgentApiKey.rotated_at,
               Operator.created_at, Operator.last_login_at, AgentWebhookEndpoint.created_at,
               AgentWebhookEndpoint.updated_at, Tenant.created_at)
    moments = [db.execute(select(func.max(c))).scalar() for c in columns]
    return max((m for m in moments if m is not None), default=None)


# The option that reads an unsigned file of 1.0.31 (manage.py).
UNSIGNED_FROM_1_0_31 = "--unsigned-from-1.0.31"


def check(db: Session, live: dict, unsigned_from_1_0_31: bool = False) -> Optional[str]:
    """Whether `live` may be replayed onto this data: raises Refused, or
    answers a warning to say (None when there is none).
    `unsigned_from_1_0_31` reads an unsigned file of 1.0.31 on an install
    with a SECRET_KEY, which is refused otherwise."""
    warning = None
    later = set(live) - _OF_1_0_31 or any(set(k) - _KEY_OF_1_0_31 for k in live.get("keys") or [])
    if live.get("signature") is not None:
        if not any(hmac.compare_digest(str(live["signature"]), _signature(live, key))
                   for key in pseudonyms.derived_keys(_SIGNS)):
            raise Refused(3, "the file is not signed by this install's SECRET_KEY, nor by a key "
                             "in SECRET_KEY_FALLBACKS. If SECRET_KEY changed since the file was "
                             "written, put the one it had in SECRET_KEY_FALLBACKS (comma separated, "
                             "newest first) and run this again; otherwise the file was changed, or "
                             "written by another install.")
    elif not live:
        pass
    elif pseudonyms.derived_key(_SIGNS) is None:
        warning = ("this install has no SECRET_KEY, so the file is neither signed nor checked: "
                   "replay only the one credentials-export wrote here, just before this restore.")
    elif not unsigned_from_1_0_31:
        raise Refused(3, "the file is not signed, and this install signs every file it writes. "
                         "If it is the file credentials-export of Korely 1.0.31 wrote for this "
                         "restore (a dump and its file made by 1.0.31, restored right after the "
                         f"upgrade), replay it with {UNSIGNED_FROM_1_0_31}; otherwise it was "
                         "changed, or written by another install.")
    elif later:
        raise Refused(3, "the file is not signed, and it holds what only Korely 1.0.32 or later "
                         f"writes, which signs it here: {UNSIGNED_FROM_1_0_31} reads only a file "
                         "of 1.0.31.")
    else:
        warning = (f"{UNSIGNED_FROM_1_0_31}: the file is read as one of Korely 1.0.31, which "
                   "signed none, so neither when it was written nor who wrote it is checked; "
                   "a rotated key and its scopes stay as the dump has them.")
    written = moment(live.get("exported_at"))
    newest = newest_moment(db) if written is not None else None
    if newest is not None and newest > written:
        raise Refused(2, f"the file was written at {written.isoformat()}, and the data here holds "
                         f"credentials changed after it (the newest at {newest.isoformat()}): "
                         "replayed, it would bring back what changed since. Replay the file "
                         "written just before this restore. If this one was already replayed "
                         "here, there is nothing left to make.")
    return warning


def export(db: Session) -> dict:
    """The state of every credential of the database, as a JSON-ready dict,
    with the moment it was read and its signature."""
    body = {
        "accounts": [str(t) for (t,) in db.query(Tenant.id).all()],
        "keys": [{"id": str(k.id), "account": str(k.tenant_id), "revoked_at": _when(k.revoked_at),
                  "project": str(k.project_id) if k.project_id else None,
                  "scopes": list(k.scopes or []), **_secret_of(k)}
                 for k in db.query(AgentApiKey).all()],
        "operators": [{"id": str(o.id), "account": str(o.tenant_id),
                       "disabled_at": _when(o.disabled_at), "password_hash": o.password_hash,
                       "install_admin": bool(o.install_admin)}
                      for o in db.query(Operator).all()],
        "webhooks": [{"id": str(w.id), "account": str(w.tenant_id), "secret": w.secret,
                      "active": bool(w.active)}
                     for w in db.query(AgentWebhookEndpoint).all()],
        **extensions.hooks.credentials_export(db),
    }
    # After the reading, by the database's clock, which wrote the data.
    body["exported_at"] = database_now(db).isoformat()
    return signed(body)


# What a rotation writes on the key's row (keys.reissue_secret): the hash of
# the secret in force, its last four for the listing, the secret it replaced
# with the end of its overlap, and the key's end, which a rotation sets.
_SECRET = ("key_hash", "last_four", "previous_key_hash", "previous_expires_at", "rotated_at",
           "expires_at")
_SECRET_TIMES = {"previous_expires_at", "rotated_at", "expires_at"}


def _secret_of(k: AgentApiKey) -> dict:
    return {c: _when(getattr(k, c)) if c in _SECRET_TIMES else getattr(k, c) for c in _SECRET}


def _as_it_was(k: AgentApiKey, was: dict, out: dict, record) -> None:
    """A live key both databases hold, as the live one had it: the secret in
    force just before the restore, as the password is (keys.reissue_secret
    wrote all of `_SECRET` at once, and they go back together), and no
    scope the live key had lost. A file without the fields (1.0.31) leaves
    the key as the dump has it."""
    if was.get("key_hash") and was["key_hash"] != k.key_hash:
        for column in _SECRET:
            value = was.get(column)
            setattr(k, column, moment(value) if column in _SECRET_TIMES else value)
        # A new end gets its warning again, as after a rotation.
        k.expiry_warned_at = None
        out["keys_secret"] += 1
        record(k.tenant_id, audit.KEY_CHANGE, str(k.id), op="rotate",
               reason="restore: rotated after the dump")
    if "scopes" in was:
        kept = [s for s in (k.scopes or []) if s in was["scopes"]]
        if kept != list(k.scopes or []):
            removed = [s for s in (k.scopes or []) if s not in kept]
            k.scopes = kept
            out["keys_narrowed"] += 1
            record(k.tenant_id, audit.KEY_CHANGE, str(k.id), op="scopes", removed=removed,
                   reason="restore: narrowed after the dump")




def replay(db: Session, live: dict, *, unsigned_from_1_0_31: bool = False) -> dict:
    """Make again, on the restored database, the credential changes the live
    one held (`live` is what `export` wrote; `{}` when it could not be read),
    and end every session and OAuth grant. Returns the counts, and the
    warning `check` said, if any; raises Refused, with nothing changed, for
    a file `check` refuses."""
    warning = check(db, live, unsigned_from_1_0_31)
    now = datetime.now(timezone.utc)
    accounts = set(live.get("accounts") or [])
    keys = {r["id"]: r for r in live.get("keys") or []}
    operators = {r["id"]: r for r in live.get("operators") or []}
    webhooks = {r["id"]: r for r in live.get("webhooks") or []}
    projects = {str(p) for (p,) in db.query(AgentProject.id).all()}
    out = {"keys_revoked": 0, "keys_moved": 0, "keys_secret": 0, "keys_narrowed": 0,
           "oauth_keys_revoked": 0, "operators_disabled": 0,
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
            continue
        if was is None:
            continue
        if was.get("revoked_at"):
            k.revoked_at = moment(was["revoked_at"])
            out["keys_revoked"] += 1
            _record(k.tenant_id, audit.KEY_REVOKE, str(k.id), reason="restore: revoked after the dump")
            continue
        if "project" in was and was["project"] != (str(k.project_id) if k.project_id else None):
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
                continue
        _as_it_was(k, was, out, _record)

    for o in db.query(Operator).all():
        was = operators.get(str(o.id))
        if was is None:
            continue
        changed = {}
        if was.get("disabled_at") and o.disabled_at is None:
            o.disabled_at = moment(was["disabled_at"])
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

    # What the Cloud's package keeps of its own (an account's login), made
    # again in the same transaction: [(count, what was made)] to print.
    db.flush()
    out["also"] = list(extensions.hooks.credentials_replay(db, live, _record))
    out["sessions_ended"] = int(db.query(OperatorSession).delete(synchronize_session=False) or 0)
    out["oauth_grants_ended"] = int(db.query(OAuthGrant).delete(synchronize_session=False) or 0)
    out["accounts_not_live"] = sum(1 for (t,) in db.query(Tenant.id).all() if str(t) not in accounts)
    out["warning"] = warning
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
