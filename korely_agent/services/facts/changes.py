"""What changed since a reader last read: the difference of agent notices
(1.0.15, their second part).

A reader's read marks (services/read_marks.py) say which facts it was handed
as true by a read of now. A change is a fact so handed that has **ended by
now** (`temporal.not_ended`), whose change was not told yet (`notified_at`):
no time of the read is compared with the closure's. A fact can carry
`closed_known_at` and still be true now, when its end is to come (an end
dated ahead, which the trigger records the day it is learned): it is no
change until that end arrives. A fact whose statement is true again is no
change while it is (`_true_again`): what the reader was handed holds. The
store never reopens a fact: given back after the deletion of what closed it
(facts/given_back.py) it is a new row of the same statement, and so is the
same thing told again.

Each change says what the reader had, what holds now when the reader may
read it, and where the closure came from (`origin`, a label and never a
threshold, so a reader can keep only the explicit ones). The origin is the
action that set the fact's end **in force**, read where that closure is
recorded (review of piece 2, 8 Nov 2026: it was read from who wrote the
fact, and from any forget record of it, so a forget that closed nothing, a
memory's past statement ending a caller's fact and a forget of a fact the
judge had closed ahead were all told as something else):

- `corrected`: a correction closed it (PATCH /v1/facts/{id}): the forget
  record of the fact written by the transaction that set its end, and the
  successor the correction wrote, which begins where the fact ends;
- `forgotten`: the fact itself was forgotten (POST /v1/facts/{id}/forget):
  that record, and no successor that ends it (a successor the judge had
  named for an end ahead, which the forget brought earlier, no longer
  begins where the fact ends);
- `stated`: a fact written by a caller ended it as its successor (POST
  /v1/facts, the `model_used` the store names a caller's fact with,
  entities.NAMED_BY_THE_CALLER);
- `read_from_memory`: every other closure. The store's reading of a memory
  (the judge, a plan come true, an edit of the memory), and every end in
  place: a past statement ends a fact in place (memory_core
  `writing._the_past`) and writes on the row only its end, a memory's as a
  caller's (POST /v1/facts with `tense: past`). The caller's has a record
  since 1.0.14, of a scope of its own, 'past' (facts/given_back.PAST): it
  keeps a deletion from giving the fact back, and it is no forget, so
  `_origin` reads the records of scope 'fact' only (a forget, a
  correction) and such an end is read for what it is, an end in place,
  never told as explicit.

A forget record is the closure in force when the transaction that wrote it
is the one that set the fact's end: the trigger writes `closed_known_at` as
that transaction's `now()` (alembic b8e4d2f6a9c1), the record's `created_at`
is the same `now()`, and a later closure moves `closed_known_at` on. A
record of a forget that closed nothing ("already_forgotten") is of another
transaction, and so is one whose end was brought earlier since. When the
fact was written after that transaction began, the trigger keeps the fact's
own `created_at` and the two differ: the closure then reads as recorded by
nobody, `read_from_memory`, never as an explicit one.

A successor ends the fact when the fact ends where the successor begins or
later: the store closes a fact at the start of what replaces it, never
before its own start (memory_core `judge.close`, Graphiti, Rasmussen et al.
2025, section 2.2.3: "setting their t_invalid to the t_valid of the
invalidating edge"). An end brought earlier since (a forget, a past
statement in place) leaves `invalidated_by_id` on the row, and it no longer
names what ended the fact. What holds now follows the same links, each to a
fact the key may read, to the one that is true now (`temporal.true_at`, the
reads' rule: begun and not ended), however long the chain; a loop ends it.

Not a change: a fact whose memory's forget closed it (that memory's forget
record, written by the transaction that set the end), and a person erased
(their facts, and the marks with them, are gone). Neither gives a
`fact.invalidated` webhook either (facts/closures.py).

The walls are those of the key that asks, now, not of the read that marked
(Collaborative Memory, Rezazadeh et al. 2025: the view filtered at the
moment of access): the account, the project, the end users the key may read
(`user_scope.wall`), for the fact and for every fact down its chain. A
successor the key may not read is, for this key, no successor: nothing that
depends on it is told (its text, its agent, its memory, its provenance
through `origin`). `more` is counted behind the same walls.

Read by set: one query finds the reader's changes, their origins and how
many there are, one walks every chain at once (a recursive query), one
reads the facts' rows. Their number does not grow with the changes asked
(review of piece 2: 1.4 ms a change, one successor and one wall at a time).
No model is asked.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import Text, and_, case, cast, exists, false, func, or_, select, tuple_
from sqlalchemy.orm import Session, aliased

from ...db.models import AgentFact, AgentForgetAudit, AgentReadMark
from .. import user_scope
from ..ids import fact_public_id, mem_public_id
from ..memory_core.presentation import context_line
from .entities import NAMED_BY_THE_CALLER, scoped_by_an_agent
from .given_back import one_triple
from .temporal import not_ended, true_at

#: The labels of `origin`.
CORRECTED, FORGOTTEN, STATED, READ_FROM_MEMORY = (
    "corrected", "forgotten", "stated", "read_from_memory")

#: The most changes one call returns.
MAX_CHANGES = 200
#: The closures a store makes on what a caller said, told of in every memory.
EXPLICIT = (CORRECTED, FORGOTTEN, STATED)


def _public(prefix: str, column):
    """SQL: a `fct_` or `mem_` id from its UUID column (ids._encode)."""
    return func.concat(prefix, func.replace(cast(column, Text), "-", ""))


#: What `changes` is, in the words of every door that takes it.
KINDS = ("Which changes the block tells a `reader`. Left out: every kind in a memory with an "
         "end user, and in an agent's memory with no end user only the explicit ones "
         "(corrected, forgotten, stated by a caller), since there the closures read from "
         "memories were mostly wrong when measured. `all`: those too.")


@dataclass
class ForARead:
    """The changes one context block may tell its reader (`for_a_read`).

    `pending`: the newest, at most CHANGES_LOOKED_AT, behind the key's walls,
    none reserved yet, of which the block tells at most CHANGES_IN_THE_BLOCK;
    `waiting`: how many wait in all, behind the same walls, or None when
    they could not be read this time (the lookup gave way
    to a lock, or failed: read_marks.in_own_session), and they wait for the
    next read. The block packs what fits and reserves it (`reserve`) before it
    is written: only what this answer reserved is told (`shown`)."""

    tenant_id: UUID
    project_id: Any
    reader: str
    pending: List[Dict[str, Any]]
    waiting: Optional[int]
    shown: List[Dict[str, Any]] = field(default_factory=list)
    gone: int = 0
    # Where the lookup stopped (`_lookup`), and what it read, to look further.
    cursor: Optional[Tuple[datetime, UUID]] = None
    end_users: Any = None
    every_kind: bool = False

    def look_further(self) -> List[Dict[str, Any]]:
        """The changes that wait after `pending`, newest first, up to
        MAX_CHANGES looked at in all: one lookup more, asked once by a
        block, with the deadlines of the first, and none when it gives way.
        The block asks after the first pass of its packing, when a place is
        free, a change of `pending` was passed over and the room left holds
        the smallest line a change can take (context._pack_changes): twenty
        changes that could not be told there, by their words or by the
        room, kept out every older one for good (the fifth and sixth
        reviews of piece 3, 9 Nov 2026)."""
        from .. import read_marks

        left = min(MAX_CHANGES, self.waiting or 0) - len(self.pending)
        if self.cursor is None or left <= 0:
            return []
        found = read_marks.in_own_session(
            "read the changes of", 0, self.tenant_id,
            lambda own: _lookup(own, self.tenant_id, self.project_id, self.reader,
                                self.end_users, left, every_kind=self.every_kind,
                                after=self.cursor))
        if found is None:
            return []
        rows, _total, _cursor = found
        return rows

    def reserve(self, packed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The changes of `packed` this answer reserved (read_marks.claim),
        in their order: the ones another answer reserved at the same time,
        or all of them when the reservation could not be made, are left
        out."""
        from .. import read_marks

        got = read_marks.claim(self.tenant_id, self.project_id, self.reader,
                               [c["fact_id"] for c in packed]) if packed else set()
        self.shown = [c for c in packed if c["fact_id"] in (got or set())]
        # Reserved, by this answer or by another: none of `packed` waits
        # any more. Not reserved at all: they all still wait.
        self.gone = len(packed) if got is not None else 0
        return self.shown

    @property
    def more(self) -> Optional[int]:
        """How many changes still wait once this answer is sent."""
        return None if self.waiting is None else max(0, self.waiting - self.gone)


def for_a_read(tenant_id: UUID, project_id: Any, reader: Optional[str], end_user: Any, *,
               known_as_of: Optional[datetime] = None,
               every_kind: bool = False) -> Optional[ForARead]:
    """The changes a context block may tell its reader (`ForARead`); None
    without a reader or for a block of the past, which tell nothing.
    `end_user` as the block reads it (context.end_users_of): the block's own
    end users, behind the key's walls. Not the block's `agent_id`: the
    changes are the reader's, of every fact it was handed, by a read of any
    agent namespace or of none, and of what it was told holds now, whoever
    wrote it; `agent_id` narrows what the block reads, `reader` names who is
    told (review of the corrections, 9 Nov 2026: a section that followed
    agent_id never told, nor counted, a change of a fact the reader had read
    outside it). The newest CHANGES_LOOKED_AT, so one too long for the
    block's room does not hold back the ones after it. Read in a session of its own with a
    mark's deadlines, as the marks are written (read_marks.in_own_session): a
    lock on the marks (a migration, an erasure) costs the block a fifth of a
    second, never the request's five, and the block then comes without them
    (review of piece 3, 8 Nov 2026)."""
    from .. import read_marks
    from ..context import CHANGES_LOOKED_AT, end_users_of

    if reader is None or not read_marks.reads_now(known_as_of=known_as_of):
        return None
    end_users = end_users_of(end_user)
    found = read_marks.in_own_session(
        "read the changes of", 0, tenant_id,
        lambda own: _lookup(own, tenant_id, project_id, reader, end_users, CHANGES_LOOKED_AT,
                            every_kind=every_kind))
    if found is None:
        return ForARead(tenant_id, project_id, reader, [], None)
    rows, total, cursor = found
    return ForARead(tenant_id, project_id, reader, rows, total, cursor=cursor,
                    end_users=end_users, every_kind=every_kind)


def handed_as_true(facts_read: List[str], block: Dict[str, Any]) -> List[str]:
    """The facts a context block handed over as true, the ones its read
    marks: its `sources` but the facts its section told the reader it had,
    history now, whose told mark the hourly purge takes. Marked again, such
    a fact had a mark with nothing told, and its change was told a second
    time (review of the corrections, 9 Nov 2026). What holds now is
    marked."""
    had = {c["fact_id"] for c in block.get("changed") or []}
    return [f for f in facts_read if f not in had]


def _readable(f, tenant_id: UUID, project_id: Any, end_users: Any):
    """SQL: the fact `f` (an alias of AgentFact) is one the key may read now:
    its account, its project, its end users."""
    return and_(f.tenant_id == tenant_id, f.project_id.is_not_distinct_from(project_id),
                user_scope.wall(f.end_user_id, end_users))


def _ends(closed, closer):
    """SQL: `closer`, the fact `closed` points at, is what ended it: `closed`
    ends where `closer` begins, or later (the module docstring)."""
    return or_(closer.valid_from.is_(None), closed.invalid_at >= closer.valid_from)


def _recorded(tenant_id: UUID, scope: str, target):
    """SQL: a forget record of `scope` names `target` and was written by the
    transaction that set the fact's end in force (the module docstring).
    Against the account's records of that scope, read once for the query."""
    A = AgentForgetAudit
    return func.coalesce(
        tuple_(target, AgentFact.closed_known_at).in_(
            select(A.target_id, A.created_at).where(A.tenant_id == tenant_id, A.scope == scope)),
        false())


def a_correction(tenant_id: UUID, closer):
    """SQL: the fact (AgentFact) was corrected by hand into `closer` (PATCH
    /v1/facts/{id}): it points at `closer`, ends where `closer` begins
    (`_ends`), and its forget record was written by the transaction that
    ended it (`_recorded`). The rule `_origin` calls CORRECTED, for a reader
    of the facts an edit of a memory must leave standing (given_back
    .standing_corrections, 1.0.27)."""
    return and_(closer.id == AgentFact.invalidated_by_id, _ends(AgentFact, closer),
                _recorded(tenant_id, "fact", _public("fct_", AgentFact.id)))
def _by_its_record(tenant_id: UUID):
    """SQL: the closure in force on the fact is a forget or a correction,
    its record of scope 'fact' written by the transaction that set it."""
    return _recorded(tenant_id, "fact", _public("fct_", AgentFact.id))


def _stated(closer):
    """SQL: the successor `closer` is a fact a caller wrote (POST /v1/facts,
    a correction: entities.NAMED_BY_THE_CALLER)."""
    return closer.model_used.in_(NAMED_BY_THE_CALLER)


def _origin(tenant_id: UUID, closer):
    """SQL: where the closure in force came from (the labels above), with
    `closer` the successor that ended the fact, joined only when the key may
    read it. One expression, read by the filter of the kinds and by the rows."""
    by_its_record = _by_its_record(tenant_id)
    return case(
        (and_(by_its_record, closer.id.isnot(None)), CORRECTED),
        (by_its_record, FORGOTTEN),
        (_stated(closer), STATED),
        else_=READ_FROM_MEMORY)


def closed_by_a_caller(tenant_id: UUID, readable: Callable[[Any], Any]):
    """SQL on AgentFact: the closure in force came from a caller, its
    `origin` one of EXPLICIT (corrected, forgotten, stated), the rule of
    `_origin` as a condition with no join: its record, or a successor that
    ended it (`_ends`) and that a caller wrote, counted only when
    `readable(successor)` (the reader's walls on that alias), as `_changes`
    joins it. Read by the context block (services/context.py), where such a
    closure has no memory of its own to show beside its fact's."""
    closer = aliased(AgentFact, name="closer")
    return or_(_by_its_record(tenant_id),
               exists().where(closer.id == AgentFact.invalidated_by_id, readable(closer),
                              _ends(AgentFact, closer), _stated(closer)))


def _true_again(now: datetime):
    """SQL: what the reader was handed is true now in another row: a row of
    the fact's triple in its memory (`given_back.one_triple`), true now
    (`temporal.true_at`), a plan when the fact was one and a fact when it was
    not (a line shows a plan "[planned, not done]", memory_core
    `presentation.context_line`): F' given back after a deletion, or the
    same triple told again. A plan come true is a change still: the reader
    had it as not done. Such a
    row is in the fact's project and of its end user, so the key may read it
    when it may read the fact. Review of piece 3 (9 Nov 2026): a fact given
    back was told as "no longer true, now: nothing"."""
    again = aliased(AgentFact, name="again")
    return exists().where(one_triple(again, AgentFact),
                          (again.tense == "planned") == (AgentFact.tense == "planned"),
                          true_at(now, again))


def of_an_agents_memory():
    """SQL: the fact is of an agent's memory with no end user, true or false
    and never NULL: `entities.scoped_by_an_agent` read as memory_core reads
    it (`candidates.about_the_subject`, `coalesce(c, false())`), so for every
    row it says what the Python rule says (memory_core `owner.agent_scoped`).
    The bare condition is NULL for a fact with no agent id, and the NOT of
    the filter of the kinds left out every change of the memory with no id
    at all, the most common one on an install of one's own (review of piece
    3, 9 Nov 2026)."""
    return func.coalesce(scoped_by_an_agent(), false())


def _changes(tenant_id: UUID, project_id: Any, reader: str, end_users: Any, every_kind: bool,
             now: datetime, limit: int, after: Optional[Tuple[datetime, UUID]] = None):
    """The reader's changes behind the walls, newest first, at most `limit`:
    (fact id, the order's key, origin, successor id, its agent, its memory,
    how many in all from `after` on), after `after` (the key and the fact id
    of the last row a lookup read: keyset paging, so a change reserved
    meanwhile moves no row from one page to the other).
    Without `every_kind`, in an agent's memory with no end user
    (`entities.scoped_by_an_agent`) only the explicit closures (`EXPLICIT`):
    there the judge's closures were mostly wrong (21 of 32 on our own team's
    memory, memory_core 6.6.0; an estimated 4 of 5 with 6.7.0), and they are
    told only to who asks for every kind, until a measure says the judge is
    right there (decided on 8 Nov 2026). In a memory with an end
    user, every kind."""
    F, M = AgentFact, AgentReadMark
    closer = aliased(AgentFact, name="closer")
    origin = _origin(tenant_id, closer)
    by_its_memory = _recorded(tenant_id, "memory", _public("mem_", F.source_memory_id))
    at = func.coalesce(F.closed_known_at, F.invalid_at)
    q = (select(F.id, at.label("at"), origin.label("origin"), closer.id.label("closer_id"),
                closer.agent_id.label("closer_agent"),
                closer.source_memory_id.label("closer_memory"),
                func.count().over().label("total"))
         .select_from(M)
         .join(F, F.id == M.fact_id)
         .outerjoin(closer, and_(closer.id == F.invalidated_by_id,
                                 _readable(closer, tenant_id, project_id, end_users),
                                 _ends(F, closer)))
         .where(M.tenant_id == tenant_id, M.project_id.is_not_distinct_from(project_id),
                M.reader == reader, M.notified_at.is_(None),
                _readable(F, tenant_id, project_id, end_users),
                ~not_ended(now), ~_true_again(now), ~by_its_memory))
    if not every_kind:
        q = q.where(~and_(of_an_agents_memory(), origin == READ_FROM_MEMORY))
    if after is not None:
        q = q.where(or_(at < after[0], and_(at == after[0], F.id > after[1])))
    return q.order_by(at.desc(), F.id).limit(limit)


def holding_now(db: Session, tenant_id: UUID, project_id: Any, end_users: Any,
                starts: List[UUID], now: datetime) -> Dict[UUID, UUID]:
    """{a successor in `starts`: the fact that holds now down its chain}, for
    every chain at once: from each fact that has ended, to the fact it points
    at when that fact ended it (`_ends`) and the key may read it, up to the
    first that is true now (`temporal.true_at`). A chain that ends without
    one, or meets a fact the key may not read, has none. No bound on the
    length: a loop repeats a row, which the query's UNION drops, and that
    ends the walk (PostgreSQL, WITH RECURSIVE)."""
    if not starts:
        return {}
    cur, nxt = aliased(AgentFact, name="cur"), aliased(AgentFact, name="nxt")
    walk = (select(AgentFact.id.label("start"), AgentFact.id.label("id"))
            .where(AgentFact.id.in_(starts)).cte("walk", recursive=True))
    walk = walk.union(
        select(walk.c.start, nxt.id)
        .select_from(walk.join(cur, cur.id == walk.c.id)
                     .join(nxt, nxt.id == cur.invalidated_by_id))
        .where(cur.invalid_at.isnot(None), cur.invalid_at <= now,
               _readable(nxt, tenant_id, project_id, end_users), _ends(cur, nxt)))
    rows = db.execute(select(walk.c.start, AgentFact.id)
                      .join(AgentFact, AgentFact.id == walk.c.id)
                      .where(true_at(now))).all()
    return {start: holds for start, holds in rows}


def _line(f: AgentFact, who: Any) -> str:
    """The fact as the context block writes it, without its bullet: `who`
    the block's own end user, as its known facts are written
    (context._pack_facts), so "The user" means the person of the question
    only (review of piece 3: in a block of every end user each change said
    "The user" of somebody else's fact)."""
    return context_line(f, who)[2:]


def words_kept(db: Session, tenant_id: UUID, facts: List[AgentFact]) -> set:
    """The ids of `facts` whose words a read of now may still say: a fact a
    caller wrote (no source memory), or one a memory still live states. A
    forgotten memory's words never come out (review of piece 2, 8 Nov 2026:
    the change of a fact replaced and then forgotten still said "was: ..."),
    by the forget's own rule: the memory and its facts drop out of every
    default read, and a fact another memory still states keeps its words
    (DELETE /v1/memories/{id}; memory_core `store.lose_the_source`). The
    forget ends the facts it alone stated; one already ended keeps the
    forgotten memory as its source, which this reads (`store.sources_of`).
    A memory erased is gone, and forgotten as well. History reads
    (`include_invalidated`, `as_of`) still show such facts, as they did."""
    from ...db.models import AgentMemory
    from ..memory_core.store import sources_of
    from .entities import TABLES

    sources: Dict[UUID, List[UUID]] = {}
    for f in facts:
        sources[f.id] = []
        for s in sources_of(TABLES, f):
            try:
                sources[f.id].append(UUID(str(s)))
            except ValueError:
                continue
    wanted = {s for ids in sources.values() for s in ids}
    live = ({m for (m,) in db.query(AgentMemory.id).filter(
        AgentMemory.tenant_id == tenant_id, AgentMemory.id.in_(wanted),
        AgentMemory.deleted_at.is_(None))} if wanted else set())
    return {i for i, ids in sources.items() if not ids or live & set(ids)}


def _when(t: Optional[datetime]) -> Optional[str]:
    return t.isoformat() if t is not None else None


def changed_since(db: Session, tenant_id: UUID, project_id: Any, reader: str, end_users: Any,
                  limit: int = 3, *, consume: bool = True,
                  every_kind: bool = False) -> Tuple[List[Dict[str, Any]], int]:
    """(the changes, how many more there are) for `reader`, newest first,
    at most `limit` (1 to MAX_CHANGES), behind the walls of the key that
    asks: its account, its project (`project_id`) and its end users now
    (`end_users`: EVERY_END_USER, EndUsers, or one end user's id). With
    `consume` the changes are reserved before they are returned
    (read_marks.claim) and do not come back: only those this call reserved
    are returned, so two callers at once share them and never both get one.
    `every_kind`: the closures read from memories too, in an agent's memory
    with no end user (`_changes`). Each fact is written as a block for
    `end_users` writes it: "The user" for the subject that is that one end
    user, the id for anybody else. No model is asked."""
    from .. import read_marks

    out, total, _cursor = _lookup(db, tenant_id, project_id, reader, end_users, limit,
                                  every_kind=every_kind)
    if not consume:
        return out, total - len(out)
    got = read_marks.claim(tenant_id, project_id, reader, [c["fact_id"] for c in out])
    if got is None:
        # Not reserved (it gave way to a lock, or failed): none is told now,
        # and every one still waits.
        return [], total
    return [c for c in out if c["fact_id"] in got], total - len(out)


def _lookup(db: Session, tenant_id: UUID, project_id: Any, reader: str, end_users: Any,
            limit: int, *, every_kind: bool = False,
            after: Optional[Tuple[datetime, UUID]] = None):
    """(the changes, how many wait from `after` on, where this lookup
    stopped): `changed_since` without the reservation, read after `after`
    (`_changes`). A fact deleted between the two queries is no change any
    more and is counted out of the waiting."""
    limit = max(1, min(int(limit), MAX_CHANGES))
    now = datetime.now(timezone.utc)
    who = end_users if isinstance(end_users, str) else None
    page = db.execute(_changes(tenant_id, project_id, reader, end_users, every_kind, now,
                               limit, after)).all()
    if not page:
        return [], 0, after
    # A forget has no successor that ends the fact (`_origin`): only the
    # closures by a successor have something that may hold now.
    holds = holding_now(db, tenant_id, project_id, end_users,
                        list({r.closer_id for r in page if r.closer_id is not None}), now)
    rows = {f.id: f for f in db.query(AgentFact).filter(
        AgentFact.tenant_id == tenant_id,
        AgentFact.id.in_({r.id for r in page} | set(holds.values())))}
    # A fact deleted since the first query (its mark went with it) is no
    # change any more, and the others are told (review of piece 3: it made
    # the lookup fail, and the block came without any of them).
    there = [r for r in page if r.id in rows]
    said = words_kept(db, tenant_id, [rows[r.id] for r in there])
    out: List[Dict[str, Any]] = []
    for r in there:
        f = rows[r.id]
        now_holds = rows.get(holds.get(r.closer_id))
        out.append({
            "fact_id": fact_public_id(f.id),
            # None: the memory that stated it was forgotten, and its words
            # with it; the change is told without them (`words_kept`).
            "was": _line(f, who) if f.id in said else None,
            "now": ({"fact_id": fact_public_id(now_holds.id), "text": _line(now_holds, who)}
                    if now_holds is not None else None),
            "ended_at": _when(f.invalid_at),
            "known_at": _when(f.closed_known_at),
            "origin": r.origin,
            "source_memory_id": (mem_public_id(r.closer_memory)
                                 if r.origin == READ_FROM_MEMORY and r.closer_memory is not None
                                 else None),
            "by_agent": r.closer_agent,
        })
    return out, page[0].total - (len(page) - len(out)), (page[-1].at, page[-1].id)
