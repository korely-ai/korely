"""What an erasure leaves of the end user it erased (11 Oct 2026, 0.1.27).

Erasing an end user deletes their memories and facts. A few rows stay, on
purpose, because they are the record of what was done: the erasure's own
audit row, the proof (GDPR Art. 5(2)); the access trail of `audit_events`,
for AUDIT_RETENTION_DAYS; the cost rows; the webhooks already delivered.
They carried the end user's id in clear, the developer's handle for a person:
personal data in pseudonymous form (Art. 4(5)), not anonymous, kept after the
person asked to be forgotten (a legal review of 11 Oct 2026).

Now the erasure replaces that id with a keyed pseudonym, HMAC-SHA256 of
`tenant:end user` under a key derived from SECRET_KEY with HKDF and a label
of its own, so the secret serves two purposes under two keys. The developer
can still prove the erasure to the person: GET /v1/audit?user_id=<their id>
computes the same pseudonym and finds the rows. A reader of the database
alone cannot tell who it was. Still personal data, never anonymous: whoever
holds the id and the key links the two, and the key is ours.

Table by table: the erasure record and the access trail take the pseudonym
(the trail never holds content, only ids and counts); the cost rows never
name an end user, which a cost does not need (audit of 19 Oct 2026: taken
off them by the erasure, the id stayed on a row written while it was open); a
delivered webhook takes the pseudonym, and one not delivered yet goes out as
it was enqueued, with the id the receiver knows, and takes the pseudonym once
it is finished (delivered, spent or dropped: `erased_since`).

Without SECRET_KEY there is no key: the id goes and nothing replaces it, so
the proof is the `audit_id` the erasure answered with. Changing SECRET_KEY
makes the earlier pseudonyms unfindable by id, as it makes a stored provider
key unreadable (README: the `SECRET_KEY` row of the table in "Install",
and "Backups").
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from ..config import get_settings
from .sentinels import EVERY_PROJECT

# The label of the derived key: a new scheme gets a new label, and the
# pseudonyms made under the old one stay what they were.
_INFO = b"korely-erasure-pseudonym-v1"
from .ids import ERASED_PREFIX as PREFIX  # noqa: E402


def derived_key(info: bytes) -> Optional[bytes]:
    """A key of its own for one purpose, derived from SECRET_KEY with HKDF
    and the purpose's label; None without SECRET_KEY. The end users'
    pseudonyms here, the Cloud's erasure records (hosted/closing.py)."""
    secret = (get_settings().secret_key or "").strip()
    if not secret:
        return None
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=info).derive(secret.encode("utf-8"))


def _key() -> Optional[bytes]:
    return derived_key(_INFO)


def pseudonym(tenant_id: Any, end_user_id: Optional[str]) -> Optional[str]:
    """The end user's pseudonym in this account: the same for the same id,
    another in another account, None without SECRET_KEY."""
    key = _key()
    if key is None or not end_user_id:
        return None
    mac = hmac.new(key, f"{tenant_id}:{end_user_id}".encode("utf-8"), hashlib.sha256)
    return PREFIX + mac.hexdigest()[:32]


# What `key_check` signs: a constant, so the value names the key and nothing
# else. A new label of the pseudonyms' key changes it too.
_CHECKED = b"korely-pseudonym-key-check"


def key_check() -> Optional[str]:
    """A value that tells this install's pseudonym key from another without
    revealing it: an HMAC of a constant under the key `pseudonym` uses, None
    without SECRET_KEY. The erasures file a restore keeps carries it
    (services/erasure_replay.py), so a replay under another key, which would
    find none of the erased end users by their pseudonym and count them as
    nothing to erase, refuses instead (audit of 19 Oct 2026: a mismatched
    SECRET_KEY failed open)."""
    key = _key()
    if key is None:
        return None
    return hmac.new(key, _CHECKED, hashlib.sha256).hexdigest()[:32]


def keyed_under(db: Session, tenant_id: Any) -> list:
    """The account ids the pseudonyms in this account's rows are keyed to:
    its own, then those of the accounts moved into it (`tenants.
    merged_tenant_ids`, written by hosted/claim.move). A moved row keeps the
    pseudonym of the account it was made in, which cannot be keyed again
    without the end user's id (audit of 19 Oct 2026: after a move GET
    /v1/audit?user_id= lost the proof of the erasure, and the trail wrote
    the erased id in clear again)."""
    from ..db import Tenant

    merged = db.query(Tenant.merged_tenant_ids).filter(Tenant.id == tenant_id).scalar()
    return [tenant_id] + list(merged or [])


def pseudonyms_of(db: Session, tenant_id: Any, end_user_id: Optional[str]) -> list:
    """Every pseudonym the end user can have in this account's rows: the
    account's own first, then one per account moved into it. Empty without
    SECRET_KEY."""
    out: list = []
    for keyed in keyed_under(db, tenant_id):
        p = pseudonym(keyed, end_user_id)
        if p and p not in out:
            out.append(p)
    return out


def as_asked(db: Session, tenant_id: Any, end_user_id: str) -> list:
    """What an audit row about this end user can carry: the id, and its
    pseudonyms once the end user was erased. For the reads by `user_id`."""
    return [end_user_id] + pseudonyms_of(db, tenant_id, end_user_id)


def after_an_erasure(db: Session, tenant_id: Any, end_user_id: str,
                     *, project_id: Any = EVERY_PROJECT, only_the_project: bool = False) -> dict:
    """In what stays after erasing `end_user_id` (within the erasure's
    project), the id becomes its pseudonym: in every place that keeps a
    record of the person (services/end_user_places.py, the one list of
    them: the erasure records, the access trail, the webhooks delivered, the
    lists of the keys and consents confined to them). The cost rows never
    name an end user (audit of 19 Oct 2026). `only_the_project`: an id
    the project only named, whose rows of no project stay. Not committed:
    the erasure's transaction commits it with the deletions."""
    from .ids import is_pseudonym

    if is_pseudonym(end_user_id):
        # Already what an erasure left: relabelling it unlinks the proof of
        # that erasure from the person (audit of 18 Oct 2026).
        return {}
    # Before anything is renamed, to the commit: a trail row written
    # meanwhile waits and reads this erasure's record (`hold`).
    hold(db, tenant_id, end_user_id)
    return rename(db, tenant_id, [end_user_id], project_id=project_id,
                  only_named=[end_user_id] if only_the_project else ())


def after_a_projects_erasure(db: Session, tenant_id: Any, project_id: Any, *,
                             held: Iterable[str], named: Iterable[str]) -> dict:
    """`after_an_erasure` for every end user a project's erasure reaches at
    once: the people it `held`, and the ids it only `named` (its trail, its
    keys' lists, its deliveries), whose rows of no project stay. Two locks,
    the project's and the rows of no project's (`hold_projects`), never one
    per id: a lock per id took one slot of the lock table each, to the end
    of the transaction, and a project with some thousands of ids, which a
    read key can add by reading, could not be erased (audit of 27 Oct
    2026). Renamed set based, `_CHUNK` ids a statement per place (every
    place ran its own statements once per id). Not committed."""
    hold_projects(db, tenant_id, [project_id, None])
    named = list(named)
    return rename(db, tenant_id, list(held) + named, project_id=project_id, only_named=named)


# How many ids one statement of a place renames.
_CHUNK = 500


def rename(db: Session, tenant_id: Any, end_user_ids: Iterable[str], *, project_id: Any,
           only_named: Iterable[str] = ()) -> dict:
    """Each id becomes its pseudonym (nothing without SECRET_KEY) in every
    place that keeps a record of a person, `_CHUNK` ids at a time. The
    caller holds the lock (`hold`, `hold_projects`)."""
    from .end_user_places import PLACES
    from .ids import is_pseudonym

    ids = sorted({u for u in end_user_ids if u and not is_pseudonym(u)})
    only = set(only_named)
    out: dict = {place.name: 0 for place in PLACES if place.pseudonymise is not None}
    for i in range(0, len(ids), _CHUNK):
        names = {u: pseudonym(tenant_id, u) for u in ids[i:i + _CHUNK]}
        for place in PLACES:
            if place.pseudonymise is not None:
                out[place.name] += place.pseudonymise(db, tenant_id, names, project_id,
                                                      only & set(names))
    return out


def _lock_key(tenant_id: Any, what: str) -> int:
    import hashlib as _h

    digest = _h.sha256(f"korely-erasure-lock:{tenant_id}:{what}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


_NO_PROJECT_GIVEN = object()


def _project_lock(project: Any) -> str:
    return f"project:{project}" if project else "project:none"


def hold(db: Session, tenant_id: Any, end_user_id: str, *, shared: bool = False,
         project: Any = _NO_PROJECT_GIVEN) -> None:
    """A lock on (account, person) to the end of the transaction, which
    orders the trail's label with an erasure of the person (audit of 20 Oct
    2026: the trail's label was read before the erasure committed). The
    erasure takes it, exclusive, before it renames what names the person
    (`after_an_erasure`); the trail, shared, after it wrote a row in clear
    and before it reads the label again (services/audit.record), so a row
    written while the erasure is open waits for its commit and reads its
    record. Two rows of the same person never wait for each other. Keyed on
    a digest of the account and the id, never stored. Postgres only.

    `project`: the trail's row is of that project (None: of none), and it
    waits for an erasure of that project too, which takes `hold_projects`
    instead of a lock per person."""
    from sqlalchemy import text

    if db.get_bind().dialect.name != "postgresql":
        return
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    db.execute(text(f"SELECT {fn}(:k)"), {"k": _lock_key(tenant_id, end_user_id)})
    if project is not _NO_PROJECT_GIVEN:
        db.execute(text(f"SELECT {fn}(:k)"), {"k": _lock_key(tenant_id, _project_lock(project))})


def hold_projects(db: Session, tenant_id: Any, projects: Iterable[Any]) -> None:
    """A project's erasure's locks, exclusive, to the end of the
    transaction: one per project whose trail rows it renames (the project,
    and None for the rows of no project), in a fixed order. A trail row of
    one of them written in clear while it is open waits for its commit
    (`hold` with `project`). Postgres only."""
    from sqlalchemy import text

    if db.get_bind().dialect.name != "postgresql":
        return
    for key in sorted({_lock_key(tenant_id, _project_lock(p)) for p in projects}):
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


def _as_project(project: Any) -> Any:
    """A row's project as the readers compare it: a UUID, None for a row of
    no project, EVERY_PROJECT for a caller that names none."""
    from uuid import UUID

    if project is EVERY_PROJECT or project is None or isinstance(project, UUID):
        return project
    try:
        return UUID(str(project))
    except ValueError:
        return None


def _counts_here(db: Session, tenant_id: Any, end_user_id: str, made_in: Any,
                 project: Any) -> bool:
    """Whether an erasure of `end_user_id` made in `made_in` (None: every
    project of the account) renames a row of `project`, by the rule the
    erasure itself follows (`after_an_erasure`): a row of the project it was
    made in, or of no project when the person lives in no other; and a row
    of a project where the id has no memory and no fact, which no live
    person there can be (a read through a key the erasure of a project moved
    to the Default one). A namesake that lives in another project keeps the
    id there (audit of 20 Oct 2026: a record made in one project relabelled every
    project's later rows)."""
    from .end_user_places import END_USER, lives_elsewhere, lives_in

    if project is EVERY_PROJECT or made_in is None or made_in == project:
        return True
    if project is None:
        return not lives_elsewhere(db, tenant_id, (END_USER, end_user_id), made_in)
    return not lives_in(db, tenant_id, end_user_id, project)


def _record_for(db: Session, tenant_id: Any, end_user_id: str, known: list, project: Any,
                *since) -> Optional[str]:
    """The pseudonym of the end user's erasure record that renames a row of
    `project` (`_counts_here`), or None. `since`: only records filed at or
    after it."""
    from ..db import AgentForgetAudit as A

    q = db.query(A.end_user_id, A.erased_in_project_id).filter(
        A.tenant_id == tenant_id, A.scope == "user", A.end_user_id.in_(known))
    if since:
        q = q.filter(A.created_at >= since[0])
    project = _as_project(project)
    for pseud, made_in in q.order_by(A.created_at.asc()).all():
        if _counts_here(db, tenant_id, end_user_id, made_in, project):
            return pseud
    return None


def as_logged(db: Session, tenant_id: Any, end_user_id: Optional[str],
              project: Any = EVERY_PROJECT) -> Optional[str]:
    """How the trail writes an end user in a row of `project`: their
    pseudonym once an erasure of them that reaches that row is on file
    (`_counts_here`), else the id. Never raises: the trail is best effort.
    services/audit.record is its one caller on the trail, for every writer.

    An erased project's end users have such a record each (scope 'user',
    projects._file_each_end_user), so a row written after that erasure, by
    a call in flight when it committed or by a later read through a key it
    moved, takes the pseudonym too (audit of 19 Oct 2026: with only the
    project's record, scope 'project' and no end user, it kept the id).
    A project erased before 0.2.0 filed no such records, and its end users
    are written as before."""
    if not end_user_id or str(end_user_id).startswith(PREFIX):
        return end_user_id
    try:
        known = pseudonyms_of(db, tenant_id, end_user_id)
        if not known:
            return end_user_id
        # The pseudonym the record on file carries: in an account another
        # moved into, the one of the account the erasure was made in.
        return _record_for(db, tenant_id, end_user_id, known, project) or end_user_id
    except Exception:  # noqa: BLE001 - the trail never breaks the call
        return end_user_id


def erased_since(db: Session, tenant_id: Any, end_user_id: Optional[str], since,
                 *, project: Any = EVERY_PROJECT) -> Optional[str]:
    """The pseudonym of `end_user_id` when an erasure of them that reaches a
    row of `project` (`_counts_here`) was filed at or after `since`, else
    None: for a row that finished after the erasure that should have renamed
    it.

    `project`: the project the row is about (a delivery's `project_id`, a
    string or None). Besides an erasure of the end user, the erasure of that
    whole project counts, since it erased every end user it held
    (projects.delete_project, filed with scope 'project', the project in
    `target_id` and no end user: until 18 Oct 2026 a delivery of it that
    finished after the erasure kept the id). Since the audit of 19 Oct 2026
    that erasure also files a scope 'user' record per end user it erased,
    found by the first condition; the second stays for the projects erased before."""
    from ..db import AgentForgetAudit as A

    known = pseudonyms_of(db, tenant_id, end_user_id)
    if not known or since is None:
        return None
    mine = _record_for(db, tenant_id, end_user_id, known, project, since)
    if mine:
        # The pseudonym of the end user's own record (in an account another
        # moved into, maybe the other account's).
        return mine
    if project is not EVERY_PROJECT and project:
        whole = (db.query(A.id).filter(A.tenant_id == tenant_id, A.scope == "project",
                                       A.target_id == str(project), A.created_at >= since)
                 .first())
        if whole is not None:
            # A project's erasure names nobody: the account's own stands in.
            return known[0]
    return None
