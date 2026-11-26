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
learned from them, the links between their objects, the marks of which of
their facts a named reader was handed) is deleted by the
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
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Iterator, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.bounded import by_key
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
    count (the section on how each place is pseudonymised). `at_once`: the
    place takes every id of the erasure in one call, not 500 at a time
    (pseudonyms.rename), because it reads its rows whole and matches the ids
    in Python: called once per block, it read them once per block."""

    name: str
    covers: Tuple[str, ...]
    holds: bool
    find: Callable[..., Iterable[str]]
    agents: Optional[Callable[..., Iterable[str]]] = None
    lock: Optional[Callable[..., Any]] = None
    erase: Optional[Callable[..., int]] = None
    erase_project: Optional[Callable[..., int]] = None
    pseudonymise: Optional[Callable[..., int]] = None
    at_once: bool = False


# ── how each place is found ─────────────────────────────────────────────────

def _models():
    from ..db.models import (AgentApiKey, AgentBatchJob, AgentEntityAlias, AgentFact,
                             AgentForgetAudit, AgentMemory, AgentObjectLink, AgentReadMark,
                             AgentWebhookDelivery, AuditEvent, OAuthGrant, PredicateAlias,
                             PredicateCluster)
    from ..core import extensions

    # A part of the product's own tables (core/extensions.Part.stores),
    # found and erased by these same functions, by name.
    parts = {model.__name__: model for part in extensions.parts()
             for _name, model in part.stores()}
    return dict(**parts, AgentApiKey=AgentApiKey, AgentBatchJob=AgentBatchJob,
                AgentEntityAlias=AgentEntityAlias, AgentFact=AgentFact,
                AgentObjectLink=AgentObjectLink, AgentReadMark=AgentReadMark,
                AgentForgetAudit=AgentForgetAudit, AgentMemory=AgentMemory,
                AgentWebhookDelivery=AgentWebhookDelivery, AuditEvent=AuditEvent,
                OAuthGrant=OAuthGrant, PredicateAlias=PredicateAlias,
                PredicateCluster=PredicateCluster)


class _ByColumn:
    """`find` for rows that carry the id in a column and a `project_id`;
    and, as SQL, what it finds (`select`), for a read that pages it."""

    def __init__(self, names: Tuple[str, ...], column: str):
        self.names, self.column = names, column

    def __call__(self, db: Session, tenant_id, within) -> Set[str]:
        m = _models()
        out: Set[str] = set()
        for name in self.names:
            model = m[name]
            col = getattr(model, self.column)
            q = db.query(col).filter(model.tenant_id == tenant_id, col.isnot(None))
            out |= {v for (v,) in within_project(q, model.project_id, within).distinct()}
        return out

    def select(self, tenant_id, within, *, end_users: Any, agent: Optional[Tuple[str, str]] = None,
               besides: Iterable[str] = ()) -> list:
        """One SELECT per model of the ids in clear its rows name, as
        `value`, behind the tenant, `within` and the end-user wall
        (`end_users`, user_scope.within_end_users), never a pseudonym an
        erasure left; `agent` (column, name): only the rows of that agent
        namespace. `besides`: models the caller reads itself."""
        from sqlalchemy import select

        from .ids import pseudonym_condition
        from .user_scope import within_end_users

        m = _models()
        out = []
        for name in self.names:
            if name in besides:
                continue
            model = m[name]
            col = getattr(model, self.column)
            q = select(col.label("value")).where(model.tenant_id == tenant_id, col.isnot(None),
                                                 ~pseudonym_condition(col))
            if agent is not None:
                q = q.where(getattr(model, agent[0]) == agent[1])
            out.append(within_end_users(within_project(q, model.project_id, within), col,
                                        end_users))
        return out


def _by_column(*names: str, column: str = END_USER) -> _ByColumn:
    """`find` for rows that carry the id in a column and a `project_id`."""
    return _ByColumn(names, column)


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


# ── an erasure made again for someone who came back ─────────────────────────
#
# An erasure takes what was there when it was made. A replay of it after a
# restore (services/erasure_replay.py) finds, for a person who wrote again
# after it (a file of erasures kept since a loss, review of 16 Nov 2026),
# both what they held before and what they wrote since: it erases, in every
# place of this one list, only the rows written up to the erasure's moment,
# and keeps the newer ones (`up_to`). Each eraser and each renamer of a
# record holds its rows to that moment through `written_up_to`, by the
# column that says when what the row holds was last written; none applies
# it when no moment is set, so every other erasure is as before.
#
# A row written once is dated by `created_at`. A row the product writes
# again in place is dated by the column that write moves: a name by `since`
# (`store.add_alias` sets it with every new value, a project's move keeps
# it on the row it makes), a read mark by `last_read_at` (its upsert). A
# fact is not dated at all: a memory restating it joins its sources on the
# same row (`store.reconfirm`), so the row of before carries what was said
# since too, and a derived row is decided by where it came from, as the
# erasure of one memory decides it (`_erase_facts`, review of 16 Nov 2026,
# round 4: a fact a memory written since restated was erased with the
# memories of before). The vocabulary, learned from the facts and recording
# none of them, follows the facts that stay (`vocabulary_behind`).

_UP_TO: ContextVar[Optional[datetime]] = ContextVar("korely_erasure_up_to", default=None)


@contextmanager
def up_to(moment: Optional[datetime]) -> Iterator[None]:
    """The erasure inside takes only the rows written at `moment` or before,
    in every place; None: every row, as any erasure."""
    token = _UP_TO.set(moment)
    try:
        yield
    finally:
        _UP_TO.reset(token)


def written_up_to(q, written):
    """`q` held to the rows whose `written` column (when the row was
    written) is not after the moment of `up_to`; `q` itself without one."""
    moment = _UP_TO.get()
    return q if moment is None else q.filter(written <= moment)


def the_moment() -> Optional[datetime]:
    """The moment of `up_to`, None without one."""
    return _UP_TO.get()


# ── how each place is erased ────────────────────────────────────────────────

def _rows(db: Session, name: str, tenant_id, who, project_id):
    """The person's rows of the model `name`, behind `project_id`; with an
    `up_to` moment, those written by then (its `created_at`). A memory is
    dated by it although an edit rewrites its text in place: the facts of
    its earlier text stay on file under the same memory (closed, `store.
    retire_the_previous_version`), and nothing on the row tells its versions
    apart, so a memory of before goes whole."""
    model = _models()[name]
    column, value = who
    q = db.query(model).filter(model.tenant_id == tenant_id, getattr(model, column) == value)
    if the_moment() is not None:
        q = written_up_to(q, model.created_at)
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
    return erase_items(db, tenant_id, _ITEM_FIELD[column], value, project_id=project_id,
                       written_by=the_moment())


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


# How many memories one statement of `_stated_since` asks about, and how many
# facts `_without_the_erased` loads at a time.
_SOURCES = 200


def _stated_since(db: Session, tenant_id, who, project_id) -> Set[Any]:
    """The ids of the person's facts, behind `project_id`, that one of their
    memories written after the moment of `up_to` states: born from it or
    restated by it (`store.source_condition`, the rule the erasure of one
    memory finds its facts by), `_SOURCES` memories a statement. The
    memories written after it are those the erasure keeps: every one of
    theirs but its own (`_rows`), so the moment has one side, the one rule
    `written_up_to` draws."""
    from ..db.bounded import STREAM_ROWS
    from sqlalchemy import and_, or_, select

    from .facts.entities import TABLES
    from .memory_core.store import source_condition

    m = _models()
    F, M = m["AgentFact"], m["AgentMemory"]
    column, value = who
    taken = select(_rows(db, "AgentMemory", tenant_id, who, project_id)
                   .with_entities(M.id).subquery().c.id)
    since = within_project(db.query(M.id).filter(M.tenant_id == tenant_id,
                                                 getattr(M, column) == value,
                                                 M.id.notin_(taken)),
                           M.project_id, project_id)
    memories = [mid for (mid,) in since.yield_per(STREAM_ROWS)]
    # Theirs, around the whole condition, which bounds its LIKE branch as
    # `source_condition(scope=)` would (a scope inside it said the same twice).
    mine = and_(F.tenant_id == tenant_id, getattr(F, column) == value)
    out: Set[Any] = set()
    for i in range(0, len(memories), _SOURCES):
        stated = or_(*[source_condition(TABLES, mid) for mid in memories[i:i + _SOURCES]])
        out |= {fid for (fid,) in within_project(db.query(F.id).filter(mine, stated),
                                                 F.project_id, project_id)}
    return out


def facts_taken(db: Session, tenant_id, who, project_id, *, kept: Optional[Set[Any]] = None):
    """The query of the person's facts an erasure takes, behind `project_id`:
    every one of them; with an `up_to` moment, those that come from what they
    held then.

    A fact is derived from the memories it names (`source_memory_id`,
    `source_memory_ids`), and a memory restating it joins its sources on the
    row of before (`store.reconfirm`), so the row's `created_at` says when the
    first of them was written, not that nothing was said since. A fact one of
    their memories written since states (`kept`, `_stated_since`) stays, as
    the erasure of one memory keeps a fact other memories still assert
    (`facts.erasure._lose_the_source`): Graphiti's `remove_episode` keeps an
    edge other episodes support and takes the episode out of its `episodes`
    (getzep/graphiti, graphiti_core/graphiti.py, read at 72b5c59; the
    episodes are the edges' provenance, Rasmussen et al. 2025,
    arXiv:2501.13956, section 2.1). Every other fact that names a memory
    goes, whenever it was written: one extracted late from a memory of
    before is that memory's. A fact that names none (posted by itself) is
    dated by its row, and goes when written by the moment, or when it is a
    correction made of the words of one of the memories that go (its
    `first_memory_id`, `facts.erasure._made_of_its_words`: what a memory
    said, in whatever row it was said again, goes). GDPR art. 17 takes the
    data of before; what was said since is not lost with it."""
    from sqlalchemy import String, and_, cast, or_, select

    m = _models()
    F, M = m["AgentFact"], m["AgentMemory"]
    column, value = who
    mine = within_project(db.query(F).filter(F.tenant_id == tenant_id,
                                             getattr(F, column) == value),
                          F.project_id, project_id)
    moment = the_moment()
    if moment is None:
        return mine
    if kept is None:
        kept = _stated_since(db, tenant_id, who, project_id)
    of_before = select(_rows(db, "AgentMemory", tenant_id, who, project_id)
                       .with_entities(M.id).subquery().c.id)
    names_none = and_(F.source_memory_id.is_(None),
                      or_(F.source_memory_ids.is_(None),
                          cast(F.source_memory_ids, String).in_(("null", "[]"))))
    written_since = and_(names_none, F.created_at > moment,
                         or_(F.first_memory_id.is_(None), F.first_memory_id.notin_(of_before)))
    q = mine.filter(~written_since)
    return q.filter(F.id.notin_(sorted(kept, key=str))) if kept else q


def _without_the_erased(db: Session, tenant_id, who, project_id, kept: Set[Any]) -> None:
    """The facts a memory written since states (`kept`) lose the person's
    memories of before from their sources (`store.lose_the_source`: the
    count, and `source_memory_id` re-pointed to a source they keep, before
    the memories go and take by the foreign key the facts that point at
    them), and `session_only` is recounted from what they keep, as after the
    erasure of one memory (`facts.erasure.erase_memory`). Their words and
    dates stay those of the row: a restatement records only its source
    (docs/guide/limits.md)."""
    from ..db.bounded import STREAM_ROWS

    from .facts.entities import TABLES
    from .facts.erasure import recount_session_only
    from .memory_core.store import lose_the_source, sources_of

    F, M = _models()["AgentFact"], _models()["AgentMemory"]
    of_before = {str(mid) for (mid,) in _rows(db, "AgentMemory", tenant_id, who, project_id)
                 .with_entities(M.id).yield_per(STREAM_ROWS)}
    ids = sorted(kept, key=str)
    changed = []
    for i in range(0, len(ids), _SOURCES):
        for f in db.query(F).filter(F.id.in_(ids[i:i + _SOURCES])).all():
            lost = [s for s in sources_of(TABLES, f) if s in of_before]
            for s in lost:
                lose_the_source(TABLES, f, s)
            if lost:
                changed.append(f)
    recount_session_only(db, changed)
    db.flush()


def _erase_facts(db: Session, tenant_id, who, project_id) -> int:
    """The person's facts (`facts_taken`). With an `up_to` moment, those a
    memory written since states lose the memories of before first
    (`_without_the_erased`), and the names the facts that go had taught are
    undone after them, read before they go (their `source_fact_id` is ON
    DELETE SET NULL), as the erasure of one memory undoes them (`facts.
    erasure.aliases_learned_from`, `undo_aliases`): a name is decided by the
    fact it came from, not by its date, so one a fact extracted late from a
    memory of before taught goes with it (review of 16 Nov 2026, round 5).
    Without a moment every name of the person goes with them (`aliases`)."""
    if the_moment() is None:
        return int(facts_taken(db, tenant_id, who, project_id)
                   .delete(synchronize_session=False) or 0)
    from .facts.erasure import aliases_learned_from, undo_aliases

    F = _models()["AgentFact"]
    kept = _stated_since(db, tenant_id, who, project_id)
    _without_the_erased(db, tenant_id, who, project_id, kept)
    taken = facts_taken(db, tenant_id, who, project_id, kept=kept)
    learned = aliases_learned_from(db, tenant_id, [fid for (fid,) in taken.with_entities(F.id)])
    n = int(taken.delete(synchronize_session=False) or 0)
    undo_aliases(db, learned)
    return n


def _erase_marks(db: Session, tenant_id, who, project_id) -> int:
    """The read marks of an erasure. An end user's: those of their facts, and
    those whose reader's name is their id. A reader is the caller's word for
    itself, and a caller that named its reader after an end user left their
    id in rows about other people's facts, which outlived the erasure
    (review of piece 1 of agent notices, 8 Nov 2026). `find` still reads the
    facts' end users only: a reader's name is not a person the project held.
    An agent namespace's: those whose reader is its name, the reads that
    agent made, which are its data as its facts are (decided on 8 Nov
    2026; the marks of its facts go with them, the cascade). Behind the
    erasure's project, both."""
    from sqlalchemy import or_

    column, value = who
    model = _models()["AgentReadMark"]
    whose = (or_(model.end_user_id == value, model.reader == value) if column == END_USER
             else model.reader == value)
    # Dated by its last read: a read again of the same fact writes the same
    # row again (read_marks, its upsert).
    q = written_up_to(db.query(model).filter(model.tenant_id == tenant_id, whose),
                      model.last_read_at)
    return int(within_project(q, model.project_id, project_id).delete(synchronize_session=False)
               or 0)


#: The same finders and erasers for the places of a part of the product
#: (core/extensions.Part.places), by the name of a model of its `stores`.
by_column = _by_column
rows = _rows
delete_rows = _delete
project_rows = _of_project


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
                n += int(written_up_to(db.query(A).filter(A.tenant_id == tenant_id,
                                                          A.project_not_recorded.is_(True),
                                                          getattr(A, column).in_(rest)),
                                       A.since)
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
    # Up to a moment (`up_to`), by when the name was last stated (`since`):
    # `store.add_alias` writes a new value on the row of before and dates it
    # there, and a project's move makes a new row and keeps the date.
    q = within_project(written_up_to(q, AgentEntityAlias.since),
                       AgentEntityAlias.project_id, project_id)
    return int(q.delete(synchronize_session=False) or 0)


def vocabulary_behind(db: Session, tenant_id, *, project_id, end_user_id: str) -> int:
    """An end user's vocabulary, behind the erasure's wall: their relations,
    and with them their phrases. Every part of it was learned from their
    facts (one vocabulary per end user of a project, 18 Oct 2026).

    Up to a moment (`up_to`), after their facts (`PLACES` erases the
    vocabulary after them): a phrase is dated by its own row, and goes when
    learned by then; a relation records no fact it was learned from, and is
    kept while a fact of theirs that stays is stored under it (the link a
    read follows, `facts.entities.predicate_condition`), else goes, whenever
    learned, with every phrase it has, those learned since too: a relation
    learned since from a fact extracted late from a memory of before is
    that memory's (review of 16 Nov 2026, round 5). Its example and its
    centroid are then decided again from the facts that stay (`facts.
    erasure._erase_examples`, after this). A relation learned before and
    used since kept its name, the name the facts that stay are stored
    under."""
    from sqlalchemy import exists, func, select

    m = _models()
    C, V, F = m["PredicateCluster"], m["PredicateAlias"], m["AgentFact"]
    q = within_project(db.query(C.id).filter(C.tenant_id == tenant_id,
                                             C.end_user_id == end_user_id),
                       C.project_id, project_id)
    if the_moment() is None:
        return _erase_clusters(db, q)
    used = exists().where(F.tenant_id == C.tenant_id,
                          F.project_id.is_not_distinct_from(C.project_id),
                          F.end_user_id.is_not_distinct_from(C.end_user_id),
                          F.predicate_canonical == C.canonical)
    n = _erase_clusters(db, q.filter(~used))
    kept = select(q.subquery().c.id)
    # One member less for each phrase that leaves its relation, as when a
    # phrase is separated from it (`store._separate`).
    leaving = written_up_to(select(func.count()).where(V.cluster_id == C.id),
                            V.created_at).scalar_subquery()
    db.query(C).filter(C.id.in_(kept)).update(
        {C.member_count: func.greatest(0, C.member_count - leaving)}, synchronize_session=False)
    written_up_to(db.query(V).filter(V.cluster_id.in_(kept)), V.created_at) \
        .delete(synchronize_session=False)
    return n


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
    q = written_up_to(db.query(A).filter(A.tenant_id == tenant_id,
                                         A.end_user_id.in_(list(names))), A.created_at)
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
    q = written_up_to(db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant_id,
                                                  col.in_(list(names))), AuditEvent.ts)
    n = int(within_project(q, AuditEvent.project_id, project_id)
            .update({col: _renamed(col, names)}, synchronize_session=False) or 0)
    if project_id is not EVERY_PROJECT and project_id is not None:
        people = [u for u in names if u not in only]
        nowhere_else = set(people) - _living_elsewhere(db, tenant_id, people, project_id)
        if nowhere_else:
            mine = {u: names[u] for u in nowhere_else}
            n += int(written_up_to(db.query(AuditEvent)
                                   .filter(AuditEvent.tenant_id == tenant_id,
                                           col.in_(list(mine)),
                                           AuditEvent.project_id.is_(None)), AuditEvent.ts)
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


def naming_any(column, names) -> Any:
    """A JSON list column holds one of `names` (Postgres `?|`)."""
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import ARRAY, JSONB
    from sqlalchemy import Text, literal

    return cast(column, JSONB).has_any(cast(literal(list(names)), ARRAY(Text)))


def _confined_keys(db: Session, tenant_id, names, project_id):
    """The keys `keys.key_project` places in the erasure's project (a key
    with no project in the Default one) whose list names one of `names`,
    found by the database, `_BLOCK` at a time, by id (audit of 27 Oct 2026:
    every confined key of the project was loaded at once)."""
    from ..db import AgentApiKey

    from .keys import within_key_project

    q = written_up_to(db.query(AgentApiKey).filter(AgentApiKey.tenant_id == tenant_id,
                                                   AgentApiKey.end_users.isnot(None),
                                                   naming_any(AgentApiKey.end_users, names)),
                      AgentApiKey.created_at)
    yield from by_key(within_key_project(db, q, AgentApiKey.project_id, tenant_id, project_id),
                      AgentApiKey.id, _BLOCK)


def _left_with_nobody(users) -> bool:
    """A key's list after an erasure holds only pseudonyms: it would see
    nobody, and the erasure revokes it. A prefix entry ("acme:*") is a rule,
    not a person, and keeps it."""
    return all(u.startswith(PREFIX) for u in users)


def keys_an_erasure_ends(db: Session, tenant_id: Any, end_user_id: str, project_id: Any) -> list:
    """The ids of the live keys the erasure of `end_user_id` in `project_id`
    would revoke (`rename_in_lists`): those confined to that end user and no
    one else living. Nothing written. Read by DELETE /v1/users/{id}/memories,
    which refuses such an erasure to the old secret of a rotation: it would
    end a key (services/keys.PREVIOUS_SECRET_CODE)."""
    stand_in = {end_user_id: PREFIX}
    return [key.id for keys in _confined_keys(db, tenant_id, [end_user_id], project_id)
            for key in keys
            if key.revoked_at is None
            and _left_with_nobody([stand_in.get(u, u) for u in key.end_users or []])]


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
    for every id of `names`, never once per id (audit of 27 Oct 2026): the
    keys and codes found by the database, `_BLOCK` ids a statement; the
    trail rows that hold a list read once each, `_BLOCK` rows at a time, and
    their entries looked up in `names`. Until 1.0.40 the trail rows were
    found by a pattern per id, `_BLOCK` patterns on every trail row of the
    project for every block of ids: a project's erasure cost its ids times
    its trail rows (audit of 27 Oct 2026, run 6). Called with every id of
    an erasure at once (`Place.at_once`)."""
    from datetime import datetime, timezone

    from ..db import AuditEvent
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
    ids = sorted(names)
    blocks = [ids[i:i + _BLOCK] for i in range(0, len(ids), _BLOCK)]
    for block in blocks:
        q = written_up_to(db.query(OAuthGrant).filter(OAuthGrant.tenant_id == tenant_id,
                                                      OAuthGrant.end_users.isnot(None),
                                                      naming_any(OAuthGrant.end_users, block)),
                          OAuthGrant.created_at)
        for grants in by_key(within_key_project(db, q, OAuthGrant.project_id, tenant_id,
                                                project_id), OAuthGrant.id, _BLOCK, lock=True):
            for grant in grants:
                users = list(grant.end_users or [])
                if not any(u in stand_in for u in users):
                    continue
                grant.end_users = swapped(users)
                if all(u.startswith(PREFIX) for u in grant.end_users) and grant.used_at is None:
                    grant.used_at = datetime.now(timezone.utc)
            db.flush()
    n = 0
    for block in blocks:
        for keys in _confined_keys(db, tenant_id, block, project_id):
            for key in keys:
                users = list(key.end_users or [])
                if not any(u in stand_in for u in users):
                    continue
                users = swapped(users)
                key.end_users = users
                if _left_with_nobody(users) and key.revoked_at is None:
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
    # Every row that holds a list, each read once, its entries looked up in
    # `names` (`listed`, the form the rewrite below replaces): no id goes
    # into the statement, so its cost is the rows, whatever the ids.
    users_text = AuditEvent.meta["users"].as_string()
    q = written_up_to(db.query(AuditEvent).filter(
        AuditEvent.tenant_id == tenant_id, users_text.isnot(None)),
        AuditEvent.ts)
    if not names:
        return n
    for rows in by_key(within_project(q, AuditEvent.project_id, project_id), AuditEvent.id,
                       _BLOCK):
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
    q = written_up_to(db.query(model).filter(model.tenant_id == tenant_id,
                                             named.in_(list(names))), model.created_at)
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

def _parts_places() -> tuple:
    from ..core import extensions

    return tuple(place for part in extensions.parts() for place in part.places())


# In the order an erasure takes them: the payloads and the memories locked
# first (`lock`), then the marks of the facts' reads, then the facts before
# the memories (a fact with no source memory has no cascade), then what was
# learned from them, then the records that only name the person.
PLACES: Tuple[Place, ...] = (
    Place("batch_items", ("agent_batch_jobs.payload",), holds=True,
          find=_batch_items("user_id"), agents=_batch_items("agent_id"), lock=_lock_items,
          erase_project=_of_project("AgentBatchJob")),
    # 1.0.15: which of the person's facts a named reader was handed (agent
    # notices, services/read_marks.py), and the reads made under their id as
    # the reader's name. Deleted, never kept by pseudonym: they record
    # nothing that was done, only what to tell a reader. Before the facts,
    # whose deletion would take them uncounted (ON DELETE CASCADE). An agent
    # namespace's: the reads made under its name (a reader is an agent's
    # name, by the rule of `agent_id`), and those of its facts with them.
    Place("read_marks", ("agent_read_marks",), holds=True,
          find=_by_column("AgentReadMark"), agents=_by_column("AgentReadMark", column="reader"),
          erase=_erase_marks,
          erase_project=_of_project("AgentReadMark")),
    Place("facts", ("agent_facts",), holds=True,
          find=_by_column("AgentFact"), agents=_by_column("AgentFact", column=AGENT), erase=_erase_facts,
          erase_project=_of_project("AgentFact")),
    # 1.0.33: the windows of a long memory and their vectors, derived from
    # its words (services/memory_windows.py), go with the memory's row by
    # their foreign key, so wherever its row is erased.
    Place("memories", ("agent_memories", "agent_memory_windows"), holds=True,
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
    # The places of a part of the product the Cloud's package carries
    # (core/extensions.Part.places), erased with the person before the
    # vocabulary; none on an install of one's own.
    *_parts_places(),
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
          find=_confined_lists, pseudonymise=_pseudonymise_lists, at_once=True),
)

# Every column that can hold text (a string, a text, JSON, an array) in a
# table no place covers whole, and that cannot hold an end user's id, one
# line each on why: a column, or a whole table when none of its columns can.
# tests/test_end_user_places.py fails on any other such column no place
# covers (audit of 27 Oct 2026: only the columns named like an end user, or
# JSON, were asked, so a text column of any other name never was).
def _parts_complete(texts: Dict[str, str]) -> Dict[str, str]:
    from ..core import extensions

    return extensions.extend("NOT_AN_END_USER", dict(texts))


_CORE_NOT_AN_END_USER: Dict[str, str] = {
    # Whole tables: the install's, the operators' and the Cloud's own rows.
    "tenants": "the account's name and the accounts merged into it (hosted/claim.py)",
    "agent_projects": "a project's name, slug, description and region, the operator's words",
    "agent_rate_limits": "a rate window's type and its truncated time bucket",
    "agent_idempotency_keys": "the SHA-256 of a write's Idempotency-Key and of its checked "
                              "request, never the key or the request; it goes with the "
                              "memory or the job it made, and with the erasure of any row "
                              "of that job (services/idempotency.py, batch._scrub)",
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
    "agent_api_keys.previous_key_hash": "the hash of the secret a rotation replaced",
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
                              "project acted on, or of another row of the store, an agent "
                              "namespace or an operator's username",
    "audit_events.ip": "the caller's address",
    "audit_events.read_ids": "the ids of what a read returned: memories, facts, and any "
                             "other row of the store it showed",
}
# The map as a part of the product the Cloud's package carries completes it,
# with the rows of its own the trail names (core/extensions.Part.extend).
NOT_AN_END_USER: Dict[str, str] = _parts_complete(_CORE_NOT_AN_END_USER)


def place(name: str) -> Place:
    return next(p for p in PLACES if p.name == name)


def places_here(db: Session) -> Tuple[Place, ...]:
    """PLACES but those whose tables the database does not have (a part's
    whose chain never ran there, db/chains.absent_tables): they hold no
    row, so nothing of them is found, locked or erased, and the rest of the
    erasure is done."""
    from ..db.chains import absent_tables

    gone = absent_tables(db)
    if not gone:
        return PLACES
    return tuple(p for p in PLACES if not {c.split(".")[0] for c in p.covers} & gone)


def found(db: Session, tenant_id, within=EVERY_PROJECT) -> Set[str]:
    """Every end user the account's rows name in clear, in every place,
    behind `within`; a pseudonym an earlier erasure left is nobody."""
    from .ids import is_pseudonym

    out: Set[str] = set()
    for p in places_here(db):
        out |= set(p.find(db, tenant_id, within))
    return {eu for eu in out if isinstance(eu, str) and eu and not is_pseudonym(eu)}


#: The holding places an end user can be in and that `held_selects` does
#: not read, each with why. GET /v1/users lists who the other places hold.
NOT_LISTED = {
    "batch_items": ("the items of a /v1/batch not imported yet: a job's payload is read "
                    "item by item from JSON, which a payload queued before 1.0.0 with a lone "
                    "surrogate makes Postgres refuse (batch.item_end_users reads such a job "
                    "one at a time, which a paged read cannot); its end users are listed "
                    "once it is imported"),
}


def held_selects(db: Session, tenant_id, within, *, end_users: Any, agent_id=None,
                 besides: Iterable[str] = ()) -> list:
    """The end users the holding places name (`Place.holds`), as SQL: one
    SELECT of `value` per place and model, behind the tenant, `within` and
    the end-user wall, never a pseudonym; with `agent_id`, only the places an
    agent namespace reaches (`Place.agents`), by its column, and only its
    rows. For GET /v1/users, which pages the union in the database: it
    listed the end users of memories and facts only, while the erasure
    takes every place here, an end user held only by a part's place
    included (review of 14 Nov 2026). `besides`: models the caller scans
    itself. The places that cannot be read this way are `NOT_LISTED`."""
    out = []
    for p in places_here(db):
        if not p.holds or p.name in NOT_LISTED:
            continue
        agent = None
        if agent_id is not None:
            if not isinstance(p.agents, _ByColumn):
                continue
            agent = (p.agents.column, agent_id)
        out += p.find.select(tenant_id, within, end_users=end_users, agent=agent,
                             besides=besides)
    return out


def held_in(db: Session, tenant_id, project_id) -> list:
    """The end users a project held data of: what its holding places name
    (`Place.holds`). Only what an earlier erasure left is skipped: an id
    written before the prefix was reserved can begin with it and is a live
    end user (audit of 17 Oct 2026)."""
    from .ids import is_pseudonym

    out: Set[str] = set()
    for p in places_here(db):
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
    for p in places_here(db):
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
    mine = [p for p in places_here(db) if p.holds and (column == END_USER or p.agents is not None)]
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
    for p in places_here(db):
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
