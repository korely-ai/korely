"""Recording what happened.

One function, called from the places that act. It exists because a buyer in a
regulated sector asks "who read this, and when" before they ask anything about
features, and because the code this replaced called a telemetry helper twenty-two
times and wrote nothing at all.

Two rules, and the second is the one people get wrong.

**Never fail the caller.** A write that succeeded must not be reported as failed
because the audit row could not be stored. But it must not be quiet either: the
failure is logged at ERROR, because an audit trail with holes is worse than none
when somebody is relying on it.

**Never record content.** No memory text, no fact objects, no query strings.
An audit row holding the data makes erasure a claim rather than a fact: delete
the memory and the log still has it. Ids, counts and namespaces only.

**What a read returned** (2026-10-01): a read records the public
ids of the memories and the facts it gave back (`read_ids`, at most MAX_IDS of
each, the counts in `meta`). The question it answers is the one asked in every
demo of a regulated buyer, "what did the agent know when it decided?": those
ids, with `as_of`, rebuild it. The same shape as the hosted product.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from ..db import AuditEvent
from .limits import MAX_ID_CHARS
from .memory_core import log_safe

logger = logging.getLogger(__name__)

# The verbs. Kept short and stable: an operator writes queries against these and
# a renamed action silently breaks a report nobody is watching.
READ = "read"
WRITE = "write"
FACT_WRITE = "fact_write"
FACT_INVALIDATE = "fact_invalidate"
ERASE = "erase"
# A memory forgotten (DELETE /v1/memories/{id} without `erase`, the MCP's
# korely_forget_memory, the dashboard's Delete): it drops out of every read
# and its text stays, until an erasure. Filed as `erase` until 1.0.23, so a
# report of `action=erase` listed memories whose text the store still held
# (review of 1 Nov 2026); Supermemory names the same act apart, "forget: the
# memory is marked as forgotten but not permanently deleted"
# (supermemory.ai/docs/api-reference/memories/forget-a-memory, read 14 Nov 2026).
FORGET = "forget"
KEY_CREATE = "key_create"
KEY_REVOKE = "key_revoke"
TENANT_CREATE = "tenant_create"
# The administration's, since 2026-10-06 (the audit of 6 October, row 8: not
# one administrative write reached the trail). A key moved to another project;
# a setting changed (the model key, the writer, a webhook, a project, an
# alias, an operator), which one in `meta.setting`; an operator who logged in.
KEY_CHANGE = "key_change"
SETTING_CHANGE = "setting_change"
LOGIN = "login"
# An operator who ended every session of theirs ("Sign out of all devices").
# It was filed as `key_revoke` of "sessions" until the audit of 17 Oct 2026:
# a reader of the trail saw a key revoked where nobody had touched a key.
LOGOUT = "logout"

_ALLOWED = {
    READ, WRITE, FACT_WRITE, FACT_INVALIDATE, ERASE, FORGET,
    KEY_CREATE, KEY_REVOKE, TENANT_CREATE, KEY_CHANGE, SETTING_CHANGE, LOGIN, LOGOUT,
}
# What the screen's Audit log offers to filter by (GET /admin/audit,
# `actions`): it kept a copy of seven of these, and an event of the other
# four could not be picked (audit of 17 Oct 2026).
ACTIONS = frozenset(_ALLOWED)

# Who acted, the values of `actor`, and what each means. GET /v1/audit
# documents them from here: it listed five while `oauth` (since 0.1.37) and
# `install` were written too, and a client that validated on the list
# refused those rows (audit of 17 Oct 2026).
ACTORS = {
    "rest": "a key over REST, or an admin key on /admin",
    "mcp": "a key over MCP",
    "dashboard": "an operator signed in to the dashboard",
    "manage": "the command line (`manage`)",
    "worker": "the background worker",
    "oauth": "an app connected over OAuth: its key made, renewed or revoked",
    "install": "a change to what is the whole install's, filed in every account's trail",
    "billing": "a notice of the install's payment provider",
}

#: The field of `meta` that names the operator behind an act of the
#: dashboard (services/usage._by) or a change to the install
#: (api/admin._audit_install): the name they sign in with, often an email.
OPERATOR_FIELD = "operator"


def meta_a_key_reads(meta: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """An event's `meta` as GET /v1/audit shows it to a key: without the
    operator's name. The row stays in the project's trail, which an erasure
    and the trail's completeness need; who signed in to the screen is the
    account's to read (/admin/audit), not every key of a project's. Since the
    dashboard reads one project at a time its reads are filed there, and a
    key of the project read `meta.operator` at every visit of the Home page
    (review of 6 Nov 2026)."""
    if not meta or OPERATOR_FIELD not in meta:
        return meta
    return {k: v for k, v in meta.items() if k != OPERATOR_FIELD} or None


# `record(project_id=...)` left out: the project is the one of `api_key_id`.
FROM_THE_KEY = object()

# A read can return hundreds of rows: the trail keeps the first ones, by id,
# and says how many there were (`meta.memories_read`, `meta.facts_read`).
MAX_IDS = 100


def _ids(values: Iterable, public) -> List[str]:
    """Public ids, once each, in the order read, at most MAX_IDS. A UUID is
    turned into its public form; a string is taken as public already."""
    out: List[str] = []
    for v in values:
        if v is None:
            continue
        s = public(v) if isinstance(v, UUID) else str(v)
        if s and s not in out:
            out.append(s[:64])
        if len(out) >= MAX_IDS:
            break
    return out


def _project_of_key(db: Session, api_key_id) -> Optional[UUID]:
    """The project a call made with this key was made in: `key_project`, the
    rule every read behind the wall uses. None without a key."""
    if api_key_id is None:
        return None
    from ..db import AgentApiKey
    from .keys import key_project

    row = db.get(AgentApiKey, api_key_id)
    return key_project(db, row) if row is not None else None


def record(
    db: Session,
    tenant_id: UUID,
    action: str,
    *,
    actor: str = "rest",
    result: str = "ok",
    api_key_id: Optional[UUID] = None,
    end_user_id: Optional[str] = None,
    target_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
    ip: Optional[str] = None,
    project_id: Any = FROM_THE_KEY,
    memories_read: Optional[Iterable] = None,
    facts_read: Optional[Iterable] = None,
    also_read: Optional[Dict[str, Iterable]] = None,
    also_counted: Optional[Dict[str, int]] = None,
) -> None:
    """Append one event. Never raises.

    `memories_read` / `facts_read`: what a read returned, as UUIDs or public
    ids. Kept by public id, at most MAX_IDS of each, with the full counts in
    `meta`. None is "this event read no such thing"; an empty list is "it
    looked and found none", which is worth keeping too. `also_read`: what a
    read returned of a part of the product (core/extensions.Part), by kind,
    as public ids, kept the same way, with its counts in `also_counted`
    (written to `meta` after the memories' and the facts').

    `project_id` files the event under the project the call was made in, which
    is what GET /v1/audit reads with a key's project. Left out, it is the
    project of `api_key_id` (every REST call passes its key); a caller with no
    key row, the MCP, passes the project it resolved.

    Written in its own session, not the caller's, for two reasons.

    The first was a bug: flushing into the request's session and letting it be
    committed downstream meant nothing was committed at all, because the request
    dependency closes without committing. Every event was lost and no error was
    raised, which is the same shape of failure this module was built to end.

    The second is the reason it stays this way. An audit trail has to record the
    attempts that failed, and a failed operation rolls its transaction back. A
    row that lives in that transaction disappears exactly when it is most worth
    having.
    """
    record_all(db, tenant_id, [dict(
        action=action, actor=actor, result=result, api_key_id=api_key_id,
        end_user_id=end_user_id, target_id=target_id, meta=meta, ip=ip, project_id=project_id,
        memories_read=memories_read, facts_read=facts_read, also_read=also_read,
        also_counted=also_counted)])


def record_all(db: Session, tenant_id: UUID, events: List[Dict[str, Any]]) -> None:
    """Append the events of one call, each with `record`'s arguments, in one
    transaction of the own session. Never raises.

    One call that is filed once per end user (the dashboard's lists,
    api/admin._filed_where_they_live) wrote each row in a session and a
    commit of its own, then settled each in another: 200 end users on a page
    were 400 commits (review of 14 Nov 2026). The rows go in together, or
    none does, and are settled together (`_settle_all`)."""
    from ..db import get_own_sessionmaker
    from .bookkeeping import AUDIT

    if not events:
        return
    # From the own sessions' pool: the caller's request holds a connection
    # of the main pool while this runs (audit of 27 Oct 2026).
    own = get_own_sessionmaker()()
    try:
        written = [_row(own, tenant_id, **e) for e in events]
        for row, _asked in written:
            own.add(row)
        own.flush()
        settle = [(row.id, asked, row.project_id) for row, asked in written
                  if asked and row.end_user_id == asked]
        own.commit()
        AUDIT.kept()
        _settle_all(own, tenant_id, settle)
    except Exception as e:
        # No re-raise: the operation the caller performed did happen, and
        # reporting it as failed would be the bigger lie. Counted for /health
        # (services/bookkeeping.py): a hole in the trail was found only by
        # reading the log (audit of 17 Oct 2026).
        logger.error("[audit] could not record %s for tenant %s: %s",
                     ",".join(sorted({str(e_.get("action")) for e_ in events})), tenant_id,
                     log_safe.stack(e))
        AUDIT.lost(len(events), e)
        try:
            own.rollback()
        except Exception:
            pass
    finally:
        own.close()


def _row(own: Session, tenant_id, *, action: str, actor: str = "rest", result: str = "ok",
         api_key_id=None, end_user_id=None, target_id=None, meta=None, ip=None,
         project_id: Any = FROM_THE_KEY, memories_read=None, facts_read=None,
         also_read=None, also_counted=None) -> tuple:
    """One event as its row, and the end user it was asked with (None
    without one), for `record_all`."""
    if action not in _ALLOWED:
        # Loud, because a typo here means a whole class of activity is
        # missing from a report that looks complete.
        logger.error("[audit] unknown action %r, recording anyway", action)
    if actor not in ACTORS:
        logger.error("[audit] unknown actor %r, recording anyway", actor)
    if project_id is FROM_THE_KEY:
        project_id = _project_of_key(own, api_key_id)
    # How the row names its end user, decided here for every writer of
    # the trail (`usage.log_v1_request`, `usage.log_admin_action`, the
    # rest): their pseudonym once an erasure of them that reaches this
    # row's project is on file (pseudonyms.as_logged). log_admin_action
    # wrote the id as given (audit of 20 Oct 2026).
    from .pseudonyms import as_logged

    asked = (end_user_id or None) and str(end_user_id)[:MAX_ID_CHARS]
    end_user_id = as_logged(own, tenant_id, asked, project_id) if asked else None
    from .ids import fact_public_id, mem_public_id

    meta = dict(meta or {})
    if isinstance(meta.get("users"), (list, tuple)):
        # A list of end users in the form the erasure rewrites it by
        # (end_user_places.recorded_users), whatever the caller passed.
        from .end_user_places import recorded_users

        meta["users"] = recorded_users(meta["users"])
    read: Dict[str, List[str]] = {}
    if memories_read is not None:
        listed = list(memories_read)
        read["memories"] = _ids(listed, mem_public_id)
        meta["memories_read"] = len(listed)
    if facts_read is not None:
        listed = list(facts_read)
        read["facts"] = _ids(listed, fact_public_id)
        meta["facts_read"] = len(listed)
    for kind, ids in (also_read or {}).items():
        read[kind] = _ids(ids, str)
    meta.update(also_counted or {})
    row = AuditEvent(
        tenant_id=tenant_id,
        api_key_id=api_key_id,
        project_id=project_id,
        actor=actor[:16],
        action=action[:32],
        result=result[:8],
        end_user_id=end_user_id,
        target_id=(target_id or None) and str(target_id)[:MAX_ID_CHARS],
        meta=_safe_meta(meta),
        read_ids=read or None,
        ip=(ip or None) and str(ip)[:64],
    )
    return row, asked


def _settle_all(own: Session, tenant_id, rows: List[tuple]) -> None:
    """The rows just written with their end user in clear (`rows`: id, end
    user, project), read again once no erasure of them is open (audit of 20
    Oct 2026: the trail's label was read before the erasure committed). The
    label was read before the insert; an erasure open meanwhile renames the
    rows that exist when it runs, and files the record the label is read
    from only at its commit, so a row that committed in between kept the id
    for AUDIT_RETENTION_DAYS. Here the rows are already committed, and the
    erasure, which holds `pseudonyms.hold` from before its rename to its
    commit, either renames them or is waited for, and its record read: each
    row is written again with its label. Taken after the insert, so the
    erasure never waits for this session; every lock of the rows of one call
    in one transaction, in one order, and one commit (review of 14 Nov 2026:
    a commit per row). A wait past a request's lock wait (core/threads.
    request_deadlines; the request waits too) writes the pseudonyms, never
    an id in clear. Without SECRET_KEY there is no pseudonym and nothing to do."""
    from sqlalchemy.exc import OperationalError

    from ..core.threads import request_deadlines
    from .pseudonyms import as_logged, hold, pseudonym

    if not rows or own.get_bind().dialect.name != "postgresql":
        return
    named = [(row_id, who, project, pseudonym(tenant_id, who)) for row_id, who, project in rows]
    named = [n for n in named if n[3] is not None]
    if not named:
        return
    try:
        request_deadlines(own)
        labels = {}
        # The person's lock, and the row's project's (a project's erasure
        # holds that instead of one lock per person: pseudonyms.hold_projects),
        # once each, in one order.
        for who, project in sorted({(w, p) for _id, w, p, _ps in named},
                                   key=lambda wp: (wp[0], str(wp[1]))):
            hold(own, tenant_id, who, shared=True, project=project)
            labels[(who, project)] = as_logged(own, tenant_id, who, project)
    except OperationalError:
        own.rollback()
        labels = {(w, p): ps for _id, w, p, ps in named}
    for row_id, who, project, _ps in named:
        label = labels[(who, project)]
        if label != who:
            own.query(AuditEvent).filter(AuditEvent.id == row_id).update(
                {AuditEvent.end_user_id: label}, synchronize_session=False)
    own.commit()


_MAX_META_LEN = 200


def _whole_entries(values) -> str:
    """A list of strings as the trail keeps it: whole entries up to the
    length a label may have, and how many were left out, never an entry cut
    in the middle. A key confined to end users records its list here
    (`meta.users`), and the erasure rewrites an erased id in it by the whole
    entry (services/pseudonyms.py): a cut one kept a prefix of the erased
    person's id for 400 days (audit of 18 Oct 2026)."""
    kept: list = []
    for i, x in enumerate(values):
        left = len(values) - i - 1
        more = f" (+{left} more)" if left else ""
        if len(str(kept + [x]) + more) > _MAX_META_LEN:
            break
        kept.append(x)
    left = len(values) - len(kept)
    return str(kept) + (f" (+{left} more)" if left else "")


def _safe_meta(meta: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Keep counts and short labels, drop anything that smells like content.

    The rule is easy to state and easy to violate by accident six months from
    now, when someone adds `{"query": q}` to a call site because it would be
    useful for debugging. Enforced here rather than trusted at twenty call
    sites.
    """
    if not meta:
        return None
    out: Dict[str, Any] = {}
    for k, v in meta.items():
        if isinstance(v, bool) or isinstance(v, int) or isinstance(v, float):
            out[k] = v
        elif isinstance(v, str):
            if len(v) > _MAX_META_LEN:
                logger.error(
                    "[audit] dropped meta field %r: %d characters is content, "
                    "not a label", k, len(v),
                )
                continue
            out[k] = v
        elif v is None:
            continue
        elif isinstance(v, (list, tuple)) and all(isinstance(x, str) for x in v):
            out[k] = _whole_entries(v)
        else:
            out[k] = str(v)[:_MAX_META_LEN]
    return out or None


def purge_expired(db: Session, keep_days: int) -> int:
    """Delete events past the retention window.

    Retention is a decision the operator makes and we cannot make for them: too
    short and an investigation has nothing to look at, too long and the table is
    itself a data-protection question. Default is 400 days, a year plus room for
    an annual audit to run late.
    """
    from datetime import datetime, timedelta, timezone

    from ..core.times import at_most_a_century

    if keep_days <= 0:
        return 0
    keep = at_most_a_century("audit_retention_days", keep_days, 1)
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep)
    n = db.query(AuditEvent).filter(AuditEvent.ts < cutoff).delete(synchronize_session=False)
    db.commit()
    if n:
        logger.info("[audit] purged %d events older than %d days", n, keep_days)
    return n
