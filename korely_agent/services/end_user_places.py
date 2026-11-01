"""Every place that can hold an end user's id or an erased person's data
(audit of 20 Oct 2026).

One registry, read by every door that erases, pseudonymises or looks for a
person. Each audit found one more column, table or order the doors
missed: an alias the 0.2.0 migration filed under another project, the list
an OAuth consent recorded, an id a refused read wrote in a project's trail,
the rows a project deletion moved before a restore. Each door kept its own
list of where a person can be, and the lists drifted. Now the places are
listed once, here, each with how it is found, how it is erased and how it is
pseudonymised, and the doors read them:

- `facts.erasure.forget_end_user` and `delete_agent` erase a person through
  `erase_person`, place by place, in the order of `PLACES`;
- `projects._erase_rows` takes the people of a project from `held_in` and
  `named_in`, and its rows through `erase_project`;
- `pseudonyms.after_an_erasure` rewrites the places that keep a record of
  the person (`Place.pseudonymise`);
- `erasure_replay._end_users` looks for a restored end user with `found`;
- the Markdown export (services/export_md.py), the one copy outside the
  database, is brought in line by `after_the_commit`.

Two kinds of place. A place that `holds` the person's data (their memories,
facts, the items of a /v1/batch payload, the aliases and the vocabulary
learned from them, the links between their objects) is deleted by the
erasure, and a row there means the project held that person. The records of
an earlier erasure of them also mean the project held them, and are kept
under their pseudonym (`erasure_records`). A place
that only names them (the access trail, the lists of the keys and consents
confined to them, the webhooks delivered) is kept, with their pseudonym in
place of the id (services/pseudonyms.py), and a row there alone does not
make them one of the project's end users: a key of a project names any id
it asks about, refused or not (audit of 20 Oct 2026: the end users of a
project counted the refused ids).

tests/test_end_user_places.py reads `Base.metadata`: a column that can hold
text (a string, a text, JSON, an array) that no place covers fails it,
unless `NOT_AN_END_USER` says, in one line, why it cannot hold an end user's
id. A second test puts a marker id in every place and in the copies the
product derives from them (a delivery still to be sent, the Markdown
export), with the same id as a namesake in another project of the account,
erases it through each door (the end user's, the project's, the agent's,
after a project's deletion), and reads every table and the export for it.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ids import ERASED_PREFIX as PREFIX
from .sentinels import EVERY_PROJECT, within_project

# Who an erasure is of: the column that names them, and the id.
END_USER = "end_user_id"
AGENT = "agent_id"
# The field of a /v1/batch item that carries the same.
_ITEM_FIELD = {END_USER: "user_id", AGENT: "agent_id"}


@dataclass(frozen=True)
class Place:
    """One place a person can be.

    `covers`: the columns it is, as "table.column", or "table" when the
    erasure deletes the whole row (every column of it goes with the person).
    `holds`: a row here means the project held the person; deleted at the
    erasure, unless the place keeps a record (`pseudonymise`, as
    `erasure_records`). Else a record that names them, pseudonymised. `agents(db, tenant_id, within)`: the agent
    namespaces the place holds too, by `agent_id`, which `delete_agent`
    erases there; None for a place no namespace reaches.

    `find(db, tenant_id, within)`: the ids in clear it names, behind
    `within` (a project, or EVERY_PROJECT). `lock(db, tenant_id, who,
    project_id)`: taken before any place is erased, in the order of
    `PLACES`. `erase(db, tenant_id, who, project_id)`: the person's rows
    deleted, a count. `erase_project(db, tenant_id, project_id)`: every row
    of a project erased. `pseudonymise(db, tenant_id, names, project_id,
    only)`: each id of `names` becomes its pseudonym there, set based, a
    count (the section on how each place is pseudonymised)."""

    name: str
    covers: Tuple[str, ...]
    holds: bool
    find: Callable[..., Iterable[str]]
    agents: Optional[Callable[..., Iterable[str]]] = None
    lock: Optional[Callable[..., Any]] = None
    erase: Optional[Callable[..., int]] = None
    erase_project: Optional[Callable[..., int]] = None
    pseudonymise: Optional[Callable[..., int]] = None


# ── how each place is found ─────────────────────────────────────────────────

def _models():
    from ..db.models import (AgentApiKey, AgentBatchJob, AgentEntityAlias, AgentFact,
                             AgentForgetAudit, AgentMemory, AgentObjectLink, AgentWebhookDelivery,
                             AuditEvent, OAuthGrant, PredicateAlias, PredicateCluster)
    return dict(AgentApiKey=AgentApiKey, AgentBatchJob=AgentBatchJob,
                AgentEntityAlias=AgentEntityAlias, AgentFact=AgentFact,
                AgentObjectLink=AgentObjectLink,
                AgentForgetAudit=AgentForgetAudit, AgentMemory=AgentMemory,
                AgentWebhookDelivery=AgentWebhookDelivery, AuditEvent=AuditEvent,
                OAuthGrant=OAuthGrant, PredicateAlias=PredicateAlias,
                PredicateCluster=PredicateCluster)


def _by_column(*names: str, column: str = END_USER):
    """`find` for rows that carry the id in a column and a `project_id`."""
    def find(db: Session, tenant_id, within):
        m = _models()
        out: Set[str] = set()
        for name in names:
            model = m[name]
            col = getattr(model, column)
            q = db.query(col).filter(model.tenant_id == tenant_id, col.isnot(None))
            out |= {v for (v,) in within_project(q, model.project_id, within).distinct()}
        return out
    return find


def _batch_items(field: str):
    def find(db: Session, tenant_id, within):
        # Read by the database from the stored payloads, never a payload
        # here (`batch.item_end_users`, audit of 20 Oct 2026).
        from .batch import item_end_users

        return item_end_users(db, tenant_id, within, field=field)
    return find


def listed(text) -> list:
    """The entries of a list as the trail keeps it (`audit._whole_entries`:
    `str(list)`, maybe followed by " (+N more)")."""
    s = str(text or "")
    end = s.rfind("]")
    try:
        value = ast.literal_eval(s[:end + 1]) if end >= 0 else []
    except (ValueError, SyntaxError):
        return []
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _confined_lists(db: Session, tenant_id, within):
    from .keys import within_key_project

    m = _models()
    out: Set[str] = set()
    for model in (m["AgentApiKey"], m["OAuthGrant"]):
        q = within_key_project(db, db.query(model.end_users).filter(
            model.tenant_id == tenant_id, model.end_users.isnot(None)),
            model.project_id, tenant_id, within)
        out |= {u for (users,) in q for u in (users or [])}
    AuditEvent = m["AuditEvent"]
    q = db.query(AuditEvent.meta["users"].as_string()).filter(
        AuditEvent.tenant_id == tenant_id, AuditEvent.meta["users"].as_string().isnot(None))
    out |= {u for (users,) in within_project(q, AuditEvent.project_id, within).distinct()
            for u in listed(users)}
    return out


def _deliveries(db: Session, tenant_id, within):
    AgentWebhookDelivery = _models()["AgentWebhookDelivery"]
    named = AgentWebhookDelivery.payload["data"]["user_id"].as_string()
    q = db.query(named).filter(AgentWebhookDelivery.tenant_id == tenant_id, named.isnot(None))
    if within is not EVERY_PROJECT:
        about = AgentWebhookDelivery.payload["project_id"].as_string()
        q = q.filter(about.is_(None) if within is None else about == str(within))
    from ..db.bounded import STREAM_ROWS

    return {v for (v,) in q.distinct().yield_per(STREAM_ROWS)}


# ── how each place is erased ────────────────────────────────────────────────

def _rows(db: Session, name: str, tenant_id, who, project_id):
    model = _models()[name]
    column, value = who
    q = db.query(model).filter(model.tenant_id == tenant_id, getattr(model, column) == value)
    return within_project(q, model.project_id, project_id)


def _of_project(name: str):
    def erase_project(db: Session, tenant_id, project_id) -> int:
        model = _models()[name]
        return int(db.query(model).filter(model.tenant_id == tenant_id,
                                          model.project_id == project_id)
                   .delete(synchronize_session=False) or 0)
    return erase_project


def _lock_items(db: Session, tenant_id, who, project_id) -> int:
    """The /v1/batch payloads first, and under the jobs' locks: a payload is
    a copy of the memories it carried, and a job still queued would import
    them again after the erasure (2026-09-28); an item the worker is
    importing right now is written before the deletes and goes with them, or
    reads as erased (audit of 17 Oct 2026)."""
    from .batch import erase_items

    column, value = who
    return erase_items(db, tenant_id, _ITEM_FIELD[column], value, project_id=project_id)


def _lock_memories(db: Session, tenant_id, who, project_id) -> None:
    """The person's memories locked: a write of them under way holds its
    memory, and its facts commit before the deletes, which then count them
    instead of the cascade taking them uncounted (audit of 17 Oct 2026)."""
    from ..db.bounded import lock_rows

    lock_rows(db, _rows(db, "AgentMemory", tenant_id, who, project_id).with_entities(
        _models()["AgentMemory"].id))


def _delete(name: str):
    def erase(db: Session, tenant_id, who, project_id) -> int:
        return int(_rows(db, name, tenant_id, who, project_id)
                   .delete(synchronize_session=False) or 0)
    return erase


def lives_elsewhere(db: Session, tenant_id, who, project_id) -> bool:
    """Whether a memory or a fact of the person is in a project of the
    account other than `project_id`."""
    m = _models()
    column, value = who
    return any(
        db.query(model.id).filter(model.tenant_id == tenant_id, getattr(model, column) == value,
                                  model.project_id.is_distinct_from(project_id)).first()
        is not None
        for model in (m["AgentMemory"], m["AgentFact"]))


def lives_in(db: Session, tenant_id, end_user_id: str, project_id) -> bool:
    """Whether a memory or a fact of the end user is in `project_id`."""
    m = _models()
    return any(
        db.query(model.id).filter(model.tenant_id == tenant_id, model.end_user_id == end_user_id,
                                  model.project_id.is_not_distinct_from(project_id)).first()
        is not None
        for model in (m["AgentMemory"], m["AgentFact"]))


def _erase_aliases(db: Session, tenant_id, who, project_id) -> int:
    """The person's declared names, behind the erasure's wall, as every
    other place of theirs. The names 0.2.0 filed under the Default project
    went to their person's project at the upgrade (migration e7c4b2d9a6f1),
    so no erasure reaches past its project for them: until then an erasure
    in Beta also deleted the Default project's names of the id, a
    namesake's with them (audit of 27 Oct 2026). The ones it could not
    place are `unplaced_names`'."""
    column, value = who
    return aliases_behind(db, tenant_id, project_id=project_id, **{column: value})


# How many people one statement of `unplaced_names` asks about.
_CHUNK = 500


def unplaced_names(db: Session, tenant_id, people: Iterable[Tuple[str, str]], project_id) -> int:
    """The names of `people`, each of whom an erasure in `project_id` is of,
    that the upgrade could not give a project (`project_not_recorded`,
    migration e7c4b2d9a6f1: made with no project up to 0.1.40, their id
    with memories or facts in two or more other projects, or in none), for
    each who now lives in no project but `project_id` (no memory, no fact
    elsewhere): the last erasure of the person takes them, wherever it is
    made, as 0.2.1 took every such name (audit of 20 Oct 2026: an alias of
    no project survived the end user's erasure). Only these rows:
    every other name stays behind its project's wall (audit of 27 Oct
    2026). Set
    based, `_CHUNK` people a statement."""
    if project_id is EVERY_PROJECT or project_id is None:
        return 0
    m = _models()
    A = m["AgentEntityAlias"]
    people = list(people)
    n = 0
    for column in (END_USER, AGENT):
        ids = sorted({v for c, v in people if c == column and v})
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            elsewhere: Set[str] = set()
            for model in (m["AgentMemory"], m["AgentFact"]):
                col = getattr(model, column)
                elsewhere |= {v for (v,) in db.query(col).filter(
                    model.tenant_id == tenant_id, col.in_(chunk),
                    model.project_id.is_distinct_from(project_id)).distinct().limit(len(chunk))}
            rest = [v for v in chunk if v not in elsewhere]
            if rest:
                n += int(db.query(A).filter(A.tenant_id == tenant_id,
                                            A.project_not_recorded.is_(True),
                                            getattr(A, column).in_(rest))
                         .delete(synchronize_session=False) or 0)
    return n


def _erase_vocabulary(db: Session, tenant_id, who, project_id) -> int:
    return vocabulary_behind(db, tenant_id, project_id=project_id, end_user_id=who[1])


def aliases_behind(db: Session, tenant_id, *, project_id, end_user_id=None, agent_id=None) -> int:
    """The aliases of an end user or an agent (all of them with neither),
    behind `project_id`'s wall."""
    AgentEntityAlias = _models()["AgentEntityAlias"]
    q = db.query(AgentEntityAlias).filter(AgentEntityAlias.tenant_id == tenant_id)
    if end_user_id is not None:
        q = q.filter(AgentEntityAlias.end_user_id == end_user_id)
    if agent_id is not None:
        q = q.filter(AgentEntityAlias.agent_id == agent_id)
    q = within_project(q, AgentEntityAlias.project_id, project_id)
    return int(q.delete(synchronize_session=False) or 0)


def vocabulary_behind(db: Session, tenant_id, *, project_id, end_user_id: str) -> int:
    """An end user's vocabulary, behind the erasure's wall: their relations,
    and with them their phrases. Every part of it was learned from their
    facts (one vocabulary per end user of a project, 18 Oct 2026)."""
    PredicateCluster = _models()["PredicateCluster"]
    q = db.query(PredicateCluster.id).filter(PredicateCluster.tenant_id == tenant_id,
                                            PredicateCluster.end_user_id == end_user_id)
    return _erase_clusters(db, within_project(q, PredicateCluster.project_id, project_id))


def _erase_project_vocabulary(db: Session, tenant_id, project_id) -> int:
    PredicateCluster = _models()["PredicateCluster"]
    return _erase_clusters(db, db.query(PredicateCluster.id).filter(
        PredicateCluster.tenant_id == tenant_id, PredicateCluster.project_id == project_id))


def _erase_clusters(db: Session, ids) -> int:
    """The relations `ids` (a query of their ids) names, and their phrases
    first, deleted by the database: the ids never come back here
    (db/bounded.py, audit of 20 Oct 2026)."""
    m = _models()
    PredicateCluster, PredicateAlias = m["PredicateCluster"], m["PredicateAlias"]
    chosen = ids.subquery()
    db.query(PredicateAlias).filter(PredicateAlias.cluster_id.in_(select(chosen.c.id))) \
        .delete(synchronize_session=False)
    return int(db.query(PredicateCluster).filter(PredicateCluster.id.in_(select(chosen.c.id)))
               .delete(synchronize_session=False) or 0)


# ── how each place is pseudonymised ─────────────────────────────────────────
#
# `pseudonymise(db, tenant_id, names, project_id, only)`: `names` maps each
# erased id to its pseudonym (None without SECRET_KEY), at most
# `pseudonyms._CHUNK` of them, set based: one statement per place for the
# whole chunk, never one per id (audit of 27 Oct 2026: the erasure's
# pseudonym pass grew faster than its ids). `only`: the ids of `names` the project only
# named, whose rows of no project stay.

def _renamed(column, names: Dict[str, Optional[str]]):
    """`column` rewritten from `names`, in the statement that updates it."""
    from sqlalchemy import case

    return case(names, value=column)


def _pseudonymise_records(db: Session, tenant_id, names, project_id, only) -> int:
    """The erasure records naming the id (an earlier forget of one of their
    memories, an erasure before 0.1.27) take the pseudonym, and the check of
    the key it was made under (`AgentForgetAudit.key_check`, audit of 20 Oct
    2026)."""
    from .pseudonyms import key_check

    A = _models()["AgentForgetAudit"]
    q = db.query(A).filter(A.tenant_id == tenant_id, A.end_user_id.in_(list(names)))
    check = key_check() if any(names.values()) else None
    return int(within_project(q, A.project_id, project_id)
               .update({A.end_user_id: _renamed(A.end_user_id, names), A.key_check: check},
                       synchronize_session=False) or 0)


def _living_elsewhere(db: Session, tenant_id, end_user_ids, project_id) -> Set[str]:
    """Which of `end_user_ids` have a memory or a fact in a project of the
    account other than `project_id` (`lives_elsewhere`, for many at once)."""
    m = _models()
    ids = list(end_user_ids)
    out: Set[str] = set()
    for model in (m["AgentMemory"], m["AgentFact"]):
        out |= {v for (v,) in db.query(model.end_user_id).filter(
            model.tenant_id == tenant_id, model.end_user_id.in_(ids),
            model.project_id.is_distinct_from(project_id)).distinct().limit(len(ids) or 1)}
    return out


def _pseudonymise_trail(db: Session, tenant_id, names, project_id, only) -> int:
    """The access trail within the erasure's project. The trail's rows of the
    whole account (the screen's reads, filed with no project: services/
    usage.log_v1_request) belong to no project, so an erasure made in one
    never reached them, and the id stayed in clear next to its pseudonym for
    AUDIT_RETENTION_DAYS (audit of 18 Oct 2026). They take it when the person
    is nowhere else in the account; while the same id still has rows in
    another project, those reads may be of that person, and they keep it.
    `only`: an id the project only named (a read of it, refused or empty)
    was never one of its people, and only the project's rows go."""
    AuditEvent = _models()["AuditEvent"]
    col = AuditEvent.end_user_id
    q = db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant_id, col.in_(list(names)))
    n = int(within_project(q, AuditEvent.project_id, project_id)
            .update({col: _renamed(col, names)}, synchronize_session=False) or 0)
    if project_id is not EVERY_PROJECT and project_id is not None:
        people = [u for u in names if u not in only]
        nowhere_else = set(people) - _living_elsewhere(db, tenant_id, people, project_id)
        if nowhere_else:
            mine = {u: names[u] for u in nowhere_else}
            n += int(db.query(AuditEvent)
                     .filter(AuditEvent.tenant_id == tenant_id, col.in_(list(mine)),
                             AuditEvent.project_id.is_(None))
                     .update({col: _renamed(col, mine)}, synchronize_session=False) or 0)
    return n


def _pseudonymise_deliveries(db: Session, tenant_id, names, project_id, only) -> int:
    return rename_in_deliveries(db, tenant_id, names, project_id,
                                _models()["AgentWebhookDelivery"])


def _pseudonymise_lists(db: Session, tenant_id, names, project_id, only) -> int:
    # Looked up at the call: tests put a search in the middle of it.
    return rename_in_lists(db, tenant_id, names, project_id)


# How many keys, codes or trail rows the erasure loads at a time.
_BLOCK = 500


def _naming_any(column, names) -> Any:
    """A JSON list column holds one of `names` (Postgres `?|`)."""
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import ARRAY, JSONB
    from sqlalchemy import Text, literal

    return cast(column, JSONB).has_any(cast(literal(list(names)), ARRAY(Text)))


def rename_in_lists(db: Session, tenant_id: Any, names: Dict[str, Optional[str]],
                    project_id: Any) -> int:
    """The keys confined to the erased end users (services/user_scope.py):
    each id leaves their list for its pseudonym, which no read or write
    matches, and a key left with nobody else is revoked, since it would see
    nobody; and the audit rows that recorded those lists (`meta.users`) take
    the pseudonym too (audit of 17 Oct 2026: both kept the id in clear, the
    key's list for ever). A prefix entry ("acme:*") is a rule, not the
    person, and stays. Returns how many keys changed.

    The OAuth codes too, which carry the list the person allowed at consent
    to the key their exchange mints (audit of 19 Oct 2026: a code kept the id
    in clear until its purge a day later, and one exchanged after the
    erasure minted a key confined to the erased id). Locked, and before the
    keys are read: an exchange holds its code until its key is written
    (oauth._exchange_code), so either it finished and its key is read below,
    or it waits and copies the list written here. A code left with nobody is
    spent, as such a key is revoked.

    The keys, codes and trail rows of the erasure's project are read once
    for every id of `names`, never once per id (audit of 27 Oct 2026)."""
    from datetime import datetime, timezone

    from sqlalchemy import or_

    from ..db import AgentApiKey, AuditEvent
    from ..db.models import OAuthGrant

    from .keys import within_key_project

    stand_in = {u: (p or PREFIX) for u, p in names.items()}

    def swapped(users):
        return [stand_in.get(u, u) for u in users]

    # The keys (and the codes that mint them) `keys.key_project` places in
    # the erasure's project, a key with no project in the Default one.
    # Only the rows that list one of them, found by the database.
    # `_BLOCK` at a time, by id (audit of 27 Oct 2026: every confined key,
    # code and trail row of the project was loaded at once).
    after = None
    while True:
        q = db.query(OAuthGrant).filter(OAuthGrant.tenant_id == tenant_id,
                                        OAuthGrant.end_users.isnot(None),
                                        _naming_any(OAuthGrant.end_users, names))
        if after is not None:
            q = q.filter(OAuthGrant.id > after)
        grants = (within_key_project(db, q, OAuthGrant.project_id, tenant_id, project_id)
                  .order_by(OAuthGrant.id).limit(_BLOCK).with_for_update().all())
        if not grants:
            break
        after = grants[-1].id
        for grant in grants:
            users = list(grant.end_users or [])
            if not any(u in stand_in for u in users):
                continue
            grant.end_users = swapped(users)
            if all(u.startswith(PREFIX) for u in grant.end_users) and grant.used_at is None:
                grant.used_at = datetime.now(timezone.utc)
        db.flush()
    n = 0
    after = None
    while True:
        q = db.query(AgentApiKey).filter(AgentApiKey.tenant_id == tenant_id,
                                         AgentApiKey.end_users.isnot(None),
                                         _naming_any(AgentApiKey.end_users, names))
        if after is not None:
            q = q.filter(AgentApiKey.id > after)
        keys = (within_key_project(db, q, AgentApiKey.project_id, tenant_id, project_id)
                .order_by(AgentApiKey.id).limit(_BLOCK).all())
        if not keys:
            break
        after = keys[-1].id
        for key in keys:
            users = list(key.end_users or [])
            if not any(u in stand_in for u in users):
                continue
            users = swapped(users)
            key.end_users = users
            if all(u.startswith(PREFIX) for u in users) and key.revoked_at is None:
                key.revoked_at = datetime.now(timezone.utc)
            n += 1
        db.flush()
    # Behind the erasure's wall, as the keys are (audit of 18 Oct 2026: a key
    # of one project rewrote the lists recorded in every other, where the
    # same id may be another person), and the id matched as written: `%` and
    # `_` in an id are characters, not a pattern. Written as the list's text
    # spells it (services/audit._whole_entries keeps `str(list)`), which is
    # the form the rewrite below replaces: an id holding a backslash, both
    # quotes or a character Python escapes is spelled with escapes there, and
    # a search for the id as written never found its row (audit of 19 Oct 2026:
    # the id stayed in clear for AUDIT_RETENTION_DAYS).
    # Entry by entry, as `user_scope.checked` stores them: a list recorded
    # as the caller spelled it before the audit of 20 Oct 2026 (an OAuth
    # consent's, with
    # a space or a character Python strips) holds the id with them, and the
    # search for its quoted form never found the row: the users listed in a
    # key's creation record survived the erasure.
    # Found by the id as the list's text prints it inside its quotes.
    users_text = AuditEvent.meta["users"].as_string()
    after = None
    while True:
        q = db.query(AuditEvent).filter(
            AuditEvent.tenant_id == tenant_id,
            or_(*[users_text.contains(repr(u)[1:-1], autoescape=True) for u in names]))
        if after is not None:
            q = q.filter(AuditEvent.id > after)
        rows = (within_project(q, AuditEvent.project_id, project_id)
                .order_by(AuditEvent.id).limit(_BLOCK).all())
        if not rows:
            break
        after = rows[-1].id
        for row in rows:
            meta = dict(row.meta or {})
            users = str(meta.get("users", ""))
            entries = listed(users)
            if not any(u.strip() in stand_in for u in entries):
                continue
            rest = users[users.rfind("]") + 1:]
            meta["users"] = str([stand_in.get(u.strip(), u) for u in entries]) + rest
            row.meta = meta
        db.flush()
    return n


def rename_in_deliveries(db: Session, tenant_id: Any, names: Dict[str, Optional[str]],
                         project_id: Any, model) -> int:
    """The deliveries whose event names an erased end user (`memory.created`:
    data.user_id), behind the erasure's wall (the project the event is
    about). A finished one, delivered or failed with every attempt spent,
    which nothing will send again, takes the pseudonym (audit of 17 Oct
    2026: those kept the id for the 30 days the finished ones are kept).
    One still to be delivered goes out with the id its receiver knows, and
    is marked (`erased_user`): it takes the pseudonym, or loses the id
    without SECRET_KEY, when it finishes (webhooks._renamed_if_erased_
    meanwhile). Until the audit of 27 Oct 2026 it was looked up by time at
    its finish, against a record whose time is its transaction's start, so
    one enqueued while the erasure was open kept the id, and without
    SECRET_KEY, with no pseudonym to look for, every one did.

    One statement decides both, row by row, on the row it locked: the
    sender writes an outcome under the delivery's row lock (webhooks.
    _write_outcome), and the statement that waited for it reads the row the
    sender committed (PostgreSQL re-reads a row it waited for, and computes
    its new values from it). Until the audit before 1.0.0 these were two
    statements, the finished rows then the others, each with its own view: a
    delivery pending at the first and delivered by the time the second had its lock
    was in neither, neither renamed nor marked, and kept the id in clear for
    WEBHOOK_DELIVERY_RETENTION_DAYS. Either the sender's commit
    comes first and the row is renamed here, or this commits first and the
    sender, which waited, reads the mark."""
    from sqlalchemy import JSON, Text, and_, case, cast, func, literal_column, or_, true
    from sqlalchemy.dialects.postgresql import JSONB

    from .webhooks import STATUS_DELIVERED, STATUS_FAILED

    named = model.payload["data"]["user_id"].as_string()
    q = db.query(model).filter(model.tenant_id == tenant_id, named.in_(list(names)))
    if project_id is not EVERY_PROJECT:
        about = model.payload["project_id"].as_string()
        q = q.filter(about.is_(None) if project_id is None else about == str(project_id))
    finished = or_(model.status == STATUS_DELIVERED,
                   and_(model.status == STATUS_FAILED, model.attempts >= model.max_attempts))
    # Rewritten by the database, never loaded (audit of 27 Oct 2026).
    p = func.coalesce(func.to_jsonb(cast(_renamed(named, names), Text)),
                      literal_column("'null'::jsonb"))
    renamed = func.jsonb_set(cast(model.payload, JSONB),
                             literal_column("'{data,user_id}'::text[]"), p)
    # Locked first, in id order, the order the sender's drop locks them in
    # (webhooks._to_send): one UPDATE locks its rows in the order it scans
    # them, and two drops of one person could deadlock with it. The UPDATE
    # after it reads the rows as the sender committed them.
    from ..db.bounded import lock_rows

    lock_rows(db, q.with_entities(model.id).order_by(model.id))
    return int(q.update({model.payload: case((finished, cast(renamed, JSON)),
                                             else_=model.payload),
                         model.erased_user: case((finished, model.erased_user), else_=true())},
                        synchronize_session=False) or 0)


def recorded_users(users) -> Optional[list]:
    """The form a list of end users is recorded in, wherever it is kept (a
    key's `end_users`, a consent's code, the trail's `meta.users`): the one
    `user_scope.checked` stores. The erasure rewrites an id in those lists
    by the stored form, so a list recorded as the caller spelled it (an
    entry with a space Python strips) kept the erased id in clear (audit of
    20 Oct 2026: the OAuth
    consent recorded the request's list). A list `checked` refuses is never
    stored, and none is recorded."""
    from .user_scope import checked

    if users is None:
        return None
    try:
        return checked(users)
    except ValueError:
        return None


# ── the registry ────────────────────────────────────────────────────────────

# In the order an erasure takes them: the payloads and the memories locked
# first (`lock`), then the facts before the memories (a fact with no source
# memory has no cascade), then what was learned from them, then the records
# that only name the person.
PLACES: Tuple[Place, ...] = (
    Place("batch_items", ("agent_batch_jobs.payload",), holds=True,
          find=_batch_items("user_id"), agents=_batch_items("agent_id"), lock=_lock_items,
          erase_project=_of_project("AgentBatchJob")),
    Place("facts", ("agent_facts",), holds=True,
          find=_by_column("AgentFact"), agents=_by_column("AgentFact", column=AGENT), erase=_delete("AgentFact"),
          erase_project=_of_project("AgentFact")),
    Place("memories", ("agent_memories",), holds=True,
          find=_by_column("AgentMemory"), agents=_by_column("AgentMemory", column=AGENT), lock=_lock_memories, erase=_delete("AgentMemory"),
          erase_project=_of_project("AgentMemory")),
    Place("aliases", ("agent_entity_aliases",), holds=True,
          find=_by_column("AgentEntityAlias"), agents=_by_column("AgentEntityAlias", column=AGENT), erase=_erase_aliases,
          erase_project=_of_project("AgentEntityAlias")),
    # memory_core 6.1.0: the links "the same as" between two objects of the
    # person's memory, with the writer's descriptions of both (facts/links.py).
    Place("object_links", ("agent_object_links",), holds=True,
          find=_by_column("AgentObjectLink"), agents=_by_column("AgentObjectLink", column=AGENT),
          erase=_delete("AgentObjectLink"), erase_project=_of_project("AgentObjectLink")),
    Place("vocabulary", ("predicate_clusters", "predicate_aliases"), holds=True,
          find=_by_column("PredicateCluster", "PredicateAlias"), erase=_erase_vocabulary,
          erase_project=_erase_project_vocabulary),
    # An earlier erasure or forget of the person in the project: they were
    # its end user, and the record stays, by pseudonym.
    Place("erasure_records", ("agent_forget_audit.end_user_id",), holds=True,
          find=_by_column("AgentForgetAudit"), pseudonymise=_pseudonymise_records),
    Place("audit_events", ("audit_events.end_user_id",), holds=False,
          find=_by_column("AuditEvent"), pseudonymise=_pseudonymise_trail),
    Place("webhooks_delivered", ("agent_webhook_deliveries.payload",), holds=False,
          find=_deliveries, pseudonymise=_pseudonymise_deliveries),
    Place("confined_keys", ("agent_api_keys.end_users", "oauth_grants.end_users",
                            "audit_events.meta"), holds=False,
          find=_confined_lists, pseudonymise=_pseudonymise_lists),
)

# Every column that can hold text (a string, a text, JSON, an array) in a
# table no place covers whole, and that cannot hold an end user's id, one
# line each on why: a column, or a whole table when none of its columns can.
# tests/test_end_user_places.py fails on any other such column no place
# covers (audit of 27 Oct 2026: only the columns named like an end user, or
# JSON, were asked, so a text column of any other name never was).
NOT_AN_END_USER: Dict[str, str] = {
    # Whole tables: the install's, the operators' and the Cloud's own rows.
    "tenants": "the account's name and the accounts merged into it (hosted/claim.py)",
    "agent_projects": "a project's name, slug, description and region, the operator's words",
    "agent_rate_limits": "a rate window's type and its truncated time bucket",
    "agent_webhook_endpoints": "the receiver's URL, its signing secret and the event names",
    "agent_llm_usage": "the cost rows: stage, provider, model, vendor, region, and the "
                       "namespace a cost was made in, never an end user (pseudonyms.py)",
    "install_settings": "the install's settings, encrypted, and the operator who set them",
    "install_state": "the install's own state, by name",
    "login_attempts": "the dashboard's sign-in attempts: an operator's username and address",
    "operators": "the dashboard's operators and their password hashes",
    "operator_sessions": "an operator's session: token hash, browser and address",
    "oauth_clients": "a connecting app's registration (name, redirect URIs) and its secret",
    "hosted_accounts": "the Cloud account's owner (email, sign-in), plan and billing ids",
    "hosted_brakes": "the superadmin's brake per provider and its reason",
    "hosted_erasures": "a closed account's owner, by digest, how it was closed, and counts",
    "hosted_polar_events": "the billing provider's event ids and types",
    "hosted_polar_subscriptions": "the billing provider's subscription and customer ids",
    "hosted_polar_unfiled": "a billing event kept unfiled: its ids, type, product, and the "
                            "order's or subscription's ids, amounts, times and our checkout's "
                            "metadata (billing.refilable), never a customer's details",
    "hosted_signups": "the network a sign-up came from, counted",
    "hosted_topups": "a top-up's order id, month and currency",
    "hosted_usage_months": "a month's label",
    # Columns of the tables a place covers in part.
    "agent_api_keys.key_hash": "the key's hash",
    "agent_api_keys.prefix": "the key's visible prefix",
    "agent_api_keys.last_four": "the key's last four characters",
    "agent_api_keys.name": "the key's name, the operator's words",
    "agent_api_keys.tier": "the key's plan tier",
    "agent_api_keys.oauth_client_id": "the connecting app's client id",
    "agent_api_keys.scopes": "the key's scope names",
    "oauth_grants.kind": "code or refresh",
    "oauth_grants.token_hash": "the code's or token's hash",
    "oauth_grants.client_id": "the connecting app's client id",
    "oauth_grants.resource": "the resource URL the app asked for",
    "oauth_grants.redirect_uri": "the app's redirect URI",
    "oauth_grants.code_challenge": "the app's PKCE challenge",
    "oauth_grants.state": "the app's opaque state, sent back to it",
    "oauth_grants.network": "the network the consent came from",
    "oauth_grants.scopes": "the scope names the person allowed",
    "agent_batch_jobs.status": "the job's state",
    "agent_batch_jobs.last_error": "the error's label (log_safe.error), never an item's field",
    "agent_batch_jobs.errors": "the failed items' index and error label (memory_write.reason), "
                               "never their fields",
    "agent_batch_jobs.created_memory_ids": "memory ids",
    "agent_forget_audit.scope": "the erasure's kind",
    "agent_forget_audit.target_id": "a memory's, fact's or project's id, or the agent namespace "
                                    "an agent's erasure erased: its record, never an end user",
    "agent_forget_audit.key_check": "the check of the pseudonym key (pseudonyms.key_check)",
    "agent_webhook_deliveries.event_type": "the event's name",
    "agent_webhook_deliveries.status": "the delivery's state",
    "agent_webhook_deliveries.last_error": "the network error or the HTTP status, never the "
                                           "receiver's answer",
    "audit_events.actor": "who called: rest, mcp, dashboard, install",
    "audit_events.action": "the verb",
    "audit_events.result": "ok or error",
    "audit_events.target_id": "the id of the key, memory, fact, job, alias, endpoint or "
                              "project acted on, an agent namespace or an operator's username",
    "audit_events.ip": "the caller's address",
    "audit_events.read_ids": "the ids of the memories and facts a read returned",
}


def place(name: str) -> Place:
    return next(p for p in PLACES if p.name == name)


def found(db: Session, tenant_id, within=EVERY_PROJECT) -> Set[str]:
    """Every end user the account's rows name in clear, in every place,
    behind `within`; a pseudonym an earlier erasure left is nobody."""
    from .ids import is_pseudonym

    out: Set[str] = set()
    for p in PLACES:
        out |= set(p.find(db, tenant_id, within))
    return {eu for eu in out if isinstance(eu, str) and eu and not is_pseudonym(eu)}


def held_in(db: Session, tenant_id, project_id) -> list:
    """The end users a project held data of: what its holding places name
    (`Place.holds`). Only what an earlier erasure left is skipped: an id
    written before the prefix was reserved can begin with it and is a live
    end user (audit of 17 Oct 2026)."""
    from .ids import is_pseudonym

    out: Set[str] = set()
    for p in PLACES:
        if p.holds:
            out |= set(p.find(db, tenant_id, project_id))
    return sorted(u for u in out if isinstance(u, str) and u and not is_pseudonym(u))


def named_in(db: Session, tenant_id, project_id) -> list:
    """The ids a project only names (its trail, its keys' lists, its
    deliveries) and holds nothing of: a read of an id, refused or empty, is
    not a person the project held."""
    held = set(held_in(db, tenant_id, project_id))
    return sorted(found(db, tenant_id, project_id) - held)


def agents_in(db: Session, tenant_id, project_id) -> list:
    """The agent namespaces a project holds data of."""
    out: Set[str] = set()
    for p in PLACES:
        if p.agents is not None:
            out |= set(p.agents(db, tenant_id, project_id))
    return sorted(a for a in out if isinstance(a, str) and a)


def erase_person(db: Session, tenant_id, who: Tuple[str, str], *, project_id: Any,
                 locked: Optional[Callable[[], Any]] = None) -> dict:
    """Erase one end user (`(END_USER, id)`) or agent namespace (`(AGENT,
    id)`) behind `project_id`: every place that holds them, locks first.
    `locked()`, when given, runs once the locks are held and before any row
    goes (`facts.erasure.delete_agent` reads there what its facts closed).
    Not committed: the door files its record and commits. Counts by place."""
    column = who[0]
    mine = [p for p in PLACES if p.holds and (column == END_USER or p.agents is not None)]
    out: Dict[str, int] = {}
    for p in mine:
        if p.lock is not None:
            n = p.lock(db, tenant_id, who, project_id)
            if isinstance(n, int):
                out[p.name] = n
    if locked is not None:
        locked()
    for p in mine:
        if p.erase is not None:
            out[p.name] = out.get(p.name, 0) + p.erase(db, tenant_id, who, project_id)
    # Their names the upgrade could not place go too, when they live nowhere
    # else now (`unplaced_names`), whether or not this project held anything
    # of them: an id with no memory or fact anywhere, only such a name, kept
    # the id and the name after its erasure in any project but the Default
    # one, while this asked only when it had deleted a row (the audit before
    # 1.0.0).
    out["aliases"] = out.get("aliases", 0) + unplaced_names(db, tenant_id, [who], project_id)
    return out


def erase_project(db: Session, tenant_id, project_id, *, people: Iterable[Tuple[str, str]] = ()
                  ) -> dict:
    """Every row of a project in the places that hold data, and the names
    the upgrade could not place of each of `people` it held (end users and
    agents) who lives in no other project (`unplaced_names`). Not
    committed."""
    out: Dict[str, int] = {}
    for p in PLACES:
        if p.erase_project is not None:
            out[p.name] = p.erase_project(db, tenant_id, project_id)
    out["aliases"] = out.get("aliases", 0) + unplaced_names(db, tenant_id, people, project_id)
    return out


def after_the_commit(db: Session, tenant_id) -> None:
    """The copy outside the database: the Markdown export of the facts
    (FACTS_EXPORT_DIR, services/export_md.py), one file per end user, brought
    in line at once after an erasure committed (the erased end user's file
    goes). Every erasure door calls it after its commit."""
    from .export_md import after_an_erasure

    after_an_erasure(db, tenant_id)
