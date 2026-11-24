"""What a retracted fact had closed, given back (decided on 31 Oct 2026).

"I live in Paris" (memory P) gives a fact F; "now I live in Zanzibar"
(memory Z) gives G, and the judge closes F (`invalid_at`, `invalidated_by_id`
= G). Z is then forgotten, erased, or edited so that it no longer says it: G
goes, and nothing the store still holds says Paris ended. P is live and still
says its words, so Paris is true again.

Never by reopening F, nor by touching it at all: the store knew Paris closed
between the closing and the deletion, and a read with `known_as_of` in
between must still answer that, closed by G (two clocks, Graphiti, Rasmussen
et al. 2025, section 2.2.3: a row's truth is never edited, a change is a new
row, as a plan that comes true is, memory_core 3.0.0). F stays as it was
learned, `invalidated_by_id` included. A NEW open row F' is written with F's
subject, relation, object, object kind and tense, stated by F's live source
memories, and it begins where what closed F stopped standing (`_begins`):

- G erased (the memory erased, or its agent namespace deleted): G never was,
  so F' begins where F ended, and Paris reads true throughout, by F until
  the closing and by F' from it;
- G kept as history (the memory forgotten, or edited so that it no longer
  says Zanzibar): G ends at the forget or at the new text's arrival, and F'
  begins there; in between Zanzibar is true alone, as the store believed it.

So the two rows are never true together, and no read needs to know of the
pair. Nothing links F' to F: the one column that says "this row goes on as
that one" is the old row's `invalidated_by_id` (a plan that came true, a
correction), and it is F's, which is not rewritten. F' is a new row of the
same triple, as the same thing told again is.

Given back only when it is safe, decided on the rows, with no model:

(a) what closed F no longer stands: F's `invalidated_by_id` is one of the
    facts retracted now (forgotten, erased or retired with the last memory
    that stated them; a fact another live memory still states loses only that
    source and stays, and keeps F closed), or a row of the same triple that
    such a fact continues (a plan that came true), or nothing (the foreign
    key's SET NULL when the closer was erased), or a fact an earlier
    deletion retracted (`_retracted`), or a row whose own end the memory
    retracted now had said (F closed by the judgement of that memory's past
    statement, which points F at the row the statement landed on: F follows
    that row, and comes back only if it does); and every memory that ended F
    in place, if one did, is retracted now (`ended_in_place_by`: forgotten,
    erased, or edited so that it no longer says it) or was before;
(b) nobody still says F is over, as the records written when F was closed
    say it, never as the clocks of the rows would suggest it: not the caller
    (a forget, a correction, or a past statement posted by hand, POST
    /v1/facts/{id}/forget, PATCH /v1/facts/{id}, POST /v1/facts with tense
    'past', their MCP tools and the dashboard's: `record_by_hand`, read by
    `_said_by_hand`), whatever end the forget moved, even none
    ("already_forgotten"); nor a live memory whose own words ended F in
    place, a later past statement ("I no longer live in Paris", or a second
    one, "Paris is not my home any more"), what its judgement closed, or an
    edit of F's own memory that no longer states it (each such memory a row
    of `agent_fact_ended_by`, written by that memory's write, `said_over`
    and previous_version.py, `_standing`). They leave on the row the
    pointer an earlier judge gave it. And something still vouches for F, with nothing live
    said otherwise since: a source memory that is live and whose current
    text was there before the store learned F had ended, or F stated by
    hand (POST /v1/facts: no source memory, the developer's statement
    stands); and no live source whose text arrived after (`_vouching`). F'
    is stated by the vouching memories only. Until 9 Nov 2026 a fact the
    judge had closed on a day ahead and that the caller forgot, a memory
    said was over or an edit took away before that day came back true when
    the successor's memory went: the pointer read as the closing (code
    review finding, P1, 8 Nov 2026). Three rounds read who ended it from the
    clocks of the rows and memories, and each found a case the clocks did
    not tell (two memories waiting together for the worker, a merge, an
    innocent memory of the same instant): what the store knew when it
    happened is now written when it happens, for every memory that says it,
    not only the first (the fourth round: a second one, waiting for the
    worker with the first, read as vouching for F once the first went);
(c) nothing open in F's memory (the account, the project and the end user,
    of any agent: `entities.memory_scope`, the judge's candidates) says
    something else of the same subject in F's relation or in its closer's
    relation (`_open_beside`). F's own canonical triple is not something
    else: open in another agent of the end user (given back to it by the
    same deletion, or told to it again since), it is the same value;
(d) the store holds no later history of F's own triple in F's agent
    (`_told_since`): a row of it begun since F ended, or still true where F'
    would begin (F given back before, open or closed since, or told again
    since). This is also what makes it happen once: F' open is such a row,
    and a second deletion finds it.
A fact is given back when what closed it is retracted: a newer fact, or a
memory that ended it in place, once nothing else that closed it stands. One
the caller said is over never is (b), whatever pointer it carries. A past
statement names, since memory_core 6.3.0, the fact of its own write that
replaced the value ("moved to Pro, it used to be Basic"), and its memory
ended Basic in place: forgotten, it gives Basic back if another live memory
still says it, with tense 'current', also when Pro begins later than the
statement was said (a date ahead in its words) and when Pro has merged since
into an older twin. An end still ahead (a closer dated ahead, retracted
before its date, F's end untouched) is given back from there, where F ends
(`_begins`), so the two are never true together; a memory that ended F in
place, forgotten or edited, said it until then, and F' begins no earlier
(the latest of them, when several did).
The rows closed before 1.0.14 carry no record of who ended them in place
(nothing wrote it), and read as closed by their pointer, as until 1.0.13.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from uuid import UUID

from sqlalchemy import and_, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...db.models import AgentFact, AgentFactEndedBy, AgentForgetAudit, AgentMemory

# How far down a chain of continuations a closer is followed: a chain of
# continuations (a plan come true, the same triple) is a few links, and a
# loop is a corrupt store, not a reason to hang a deletion.
_CHAIN = 50


@dataclass
class Closer:
    """What closed one fact: the relations of its closer (for (c)), the
    ids from the closer down to the fact retracted now (`chain`: the closer
    alone, or a row of its triple continued by the retracted one), whose
    ends say where the fact begins again (`_begins`); and, when the memory
    that ended it in place is retracted now (`words`), until when its words
    stood: its forget, or the arrival of the text that no longer says it
    (`until`; None when it is erased, and never was)."""

    relations: Set[str] = field(default_factory=set)
    chain: Set[UUID] = field(default_factory=set)
    words: bool = False
    until: Optional[datetime] = None


@dataclass
class Closed:
    """The facts the retracted ones closed, read before the retracted rows
    go (an erased fact's closings lose their `invalidated_by_id` by the
    foreign key): `facts` {closed fact id: its Closer}, `through` every id
    that counts as "no longer stands" for (a); `words_of` the memories whose
    words are retracted now (`ended_in_place_by`), whose own clock is
    `Closer.until`."""

    facts: Dict[UUID, Closer] = field(default_factory=dict)
    through: Set[UUID] = field(default_factory=set)
    words_of: Set[UUID] = field(default_factory=set)


def _held(canonical, label):
    """A canonical name as the store reads it: the column, else (NULL, which
    the column allows) the label, which is what the column's default writes
    (`AgentFact.object_canonical`) and how `reads`, memory_core `candidates`
    and `twins` read a NULL (`coalesce`). The label is NOT NULL, so this
    never is. `_held_in` is the same on the columns, in SQL."""
    return canonical if canonical is not None else label


def _held_in(canonical, label):
    return func.lower(func.coalesce(canonical, label))


def _same_triple(a: AgentFact, b: AgentFact) -> bool:
    """The canonical triple, compared as the open-triple index compares it
    (lowercased names, `uq_agent_facts_one_open_per_triple`), a NULL name
    read as its label (`_held`)."""
    def low(v):
        return v.strip().lower()

    return (low(_held(a.subject_canonical, a.subject_label))
            == low(_held(b.subject_canonical, b.subject_label))
            and a.predicate_canonical == b.predicate_canonical
            and low(_held(a.object_canonical, a.object_label))
            == low(_held(b.object_canonical, b.object_label)))


def what_they_closed(db: Session, tenant_id: UUID, retracted: Any, *,
                     within: Callable[[Any], Any], ended: Iterable[AgentFact] = (),
                     until: Optional[datetime] = None, by: Iterable[UUID] = ()) -> Closed:
    """The closed facts whose closer is one of `retracted` (fact rows, ids,
    or a select of ids), on a query of AgentFact narrowed by `within` (the
    retracted facts' memory, `entities.memory_scope`, or the erasure's
    project, `sentinels.within_project`), and the facts `ended`, those the
    memories `by`, retracted now, had ended in place (`ended_in_place_by`),
    whose words stood until `until` (None: erased). A row of the same
    triple a retracted fact continues (a plan that came true) is followed to
    what IT closed, and is not itself given back. Read only; call it before
    the retracted rows are deleted."""
    if isinstance(retracted, (list, tuple, set, frozenset)):
        ids = {r.id if isinstance(r, AgentFact) else r for r in retracted}
        frontier: Any = list(ids) if ids else None
    else:
        ids, frontier = set(), retracted
    db.flush()
    out = Closed(through=set(ids), words_of=set(by))
    closers: Dict[UUID, AgentFact] = {}
    below: Dict[UUID, Set[UUID]] = {}   # a followed row: it and what continues it
    for _ in range(_CHAIN if frontier is not None else 0):
        rows = (within(db.query(AgentFact))
                .filter(AgentFact.tenant_id == tenant_id,
                        AgentFact.invalidated_by_id.in_(frontier),
                        AgentFact.invalid_at.isnot(None))
                .all())
        missing = {r.invalidated_by_id for r in rows} - set(closers)
        if missing:
            closers.update({c.id: c for c in db.query(AgentFact)
                            .filter(AgentFact.id.in_(list(missing))).all()})
        out.through |= {r.invalidated_by_id for r in rows}
        nxt = []
        for r in rows:
            closer = closers.get(r.invalidated_by_id)
            chain = below.get(r.invalidated_by_id, {r.invalidated_by_id})
            if r.id in out.through or r.id in out.facts:
                continue
            if closer is not None and _same_triple(r, closer):
                nxt.append(r.id)       # continued by the closer: follow it
                out.through.add(r.id)
                below[r.id] = {r.id} | chain
            else:
                c = out.facts.setdefault(r.id, Closer())
                c.chain |= chain
                if closer is not None:
                    c.relations.add(closer.predicate_canonical)
        if not nxt:
            break
        frontier = nxt
    _the_words_retracted(db, out, ended, until)
    for i in out.through:
        out.facts.pop(i, None)
    return out


def _the_words_retracted(db: Session, out: Closed, ended: Iterable[AgentFact],
                         until: Optional[datetime]) -> None:
    """The facts a memory retracted now had ended in place join `out`: the
    fact its row points at, if any, closed it too (its relation for (c), its
    end for `_begins`), and (a) asks whether it still stands."""
    rows = [r for r in ended if r.invalid_at is not None]
    pointed = {r.invalidated_by_id for r in rows if r.invalidated_by_id is not None}
    relation = dict(db.query(AgentFact.id, AgentFact.predicate_canonical)
                    .filter(AgentFact.id.in_(list(pointed))).all()) if pointed else {}
    for r in rows:
        c = out.facts.setdefault(r.id, Closer())
        c.words, c.until = True, until
        if r.invalidated_by_id is not None:
            c.chain.add(r.invalidated_by_id)
            if r.invalidated_by_id in relation:
                c.relations.add(relation[r.invalidated_by_id])


def after_retracting(db: Session, tenant_id: UUID, retracted: List[AgentFact], scope: Any,
                     *, erased: bool = False, ended: Iterable[AgentFact] = (),
                     until: Optional[datetime] = None, by: Iterable[UUID] = ()) -> int:
    """What the facts `retracted` now (forgotten, erased or retired with the
    last memory that stated them) had closed, and the facts `ended` that the
    memories `by`, retracted now, had ended in place (`ended_in_place_by`,
    their words standing until `until`), given back where nothing else
    closes them;
    `scope` the memory's (`entities.memory_scope`), where the judge's
    candidates were. `erased`: the retracted rows are deleted in this
    transaction, after this. Not committed; returns how many."""
    ended = list(ended)
    if not retracted and not ended:
        return 0
    closed = what_they_closed(db, tenant_id, retracted, within=lambda q: q.filter(scope),
                              ended=ended, until=until, by=by)
    ids = [f.id for f in retracted]
    return len(give_back(db, closed, retracted=ids, erased=ids if erased else ()))


# ── The records a deletion reads, written when the closing happens ─────────

#: The scopes of agent_forget_audit that are the caller's word on one fact
#: (`target_id` its public id): 'fact', a forget or a correction (since
#: 0.1.1); PAST, a past statement of the fact posted by hand (since 1.0.14),
#: which is not a forget, and has a scope of its own so that a reader of the
#: records can tell one from the other.
PAST = "past"
BY_HAND = ("fact", PAST)


def record_by_hand(db: Session, tenant_id: UUID, fact: AgentFact, *, invalidated: int,
                   scope: str = "fact") -> AgentForgetAudit:
    """The caller's own word that `fact` is over, in the caller's
    transaction: a forget (`erasure.forget_fact`) or a correction
    (`service.correct_fact`), scope 'fact'; or a past statement posted by
    hand that landed on it (`service.write_fact_direct`, POST /v1/facts with
    tense 'past': memory_core `writing._the_past` closes the row it
    restates, or adds to the closed row it is history of), scope PAST. Its
    record in agent_forget_audit names the fact's public id, and
    `_said_by_hand` reads it: such a fact is never given back.
    `invalidated`: 1 when this call closed it, 0 when it was over already."""
    from ..ids import fact_public_id

    audit = AgentForgetAudit(
        tenant_id=tenant_id,
        end_user_id=fact.end_user_id,
        # The project of the fact, which is the key's (every caller loads
        # it walled): deleting the project moves this row with the rest.
        project_id=fact.project_id,
        scope=scope,
        target_id=fact_public_id(fact.id),
        memories_forgotten=0,
        facts_invalidated=invalidated,
    )
    db.add(audit)
    return audit


def said_over(db: Session, memory: Any, written: Tuple[Optional[AgentFact], List[Any]],
              tense: Optional[str], landed: Set[UUID]) -> None:
    """One statement of `memory`'s write: `written` is the row memory_core
    wrote it on and what that write closed (`worker.write_memory`'s
    `write_one`). A past statement says facts are over: the row it lands on
    when that row was there before the write (memory_core
    `writing._the_past`: the open row it restates, which it closes when it
    was said, or the closed row it is history of), and each fact its
    judgement closed (`writing._judge_the_past`: the facts of the same two
    entities). The memory is one that ended each of them in place
    (`agent_fact_ended_by`), written now, whichever memory said it first: a
    second memory that says it again keeps it closed as well. `landed`
    gathers the facts this text says are over, a row born of it too, for
    facts/previous_version.py."""
    from . import closures
    from ..memory_core.fact_time import effective_tense

    fact, closed = written
    if fact is None or effective_tense(tense) != "past":
        return
    landed.add(fact.id)
    landed.update(closed or ())
    ended_by_now(db, memory, [i for i in [fact.id, *(closed or ())]
                              if not closures.born_here(db, i)])


def ended_by_now(db: Session, memory: Any, facts: Iterable[Any]) -> None:
    """`memory` ended `facts` (rows or ids) in place now: its row in
    `agent_fact_ended_by` for each, once."""
    for i in {getattr(f, "id", f) for f in facts}:
        if db.get(AgentFactEndedBy, (i, memory.id)) is None:
            db.add(AgentFactEndedBy(fact_id=i, memory_id=memory.id, tenant_id=memory.tenant_id))
    db.flush()


def no_longer_said(db: Session, memory: Any, facts: Iterable[AgentFact]) -> None:
    """`memory`'s word that `facts` are over, taken back (an edit that no
    longer says it, facts/previous_version.py)."""
    ids = [f.id for f in facts]
    if ids:
        (db.query(AgentFactEndedBy)
           .filter(AgentFactEndedBy.tenant_id == memory.tenant_id,
                   AgentFactEndedBy.memory_id == memory.id, AgentFactEndedBy.fact_id.in_(ids))
           .delete(synchronize_session=False))


def ended_in_place_by(db: Session, tenant_id: UUID, memories: Any,
                      within: Callable[[Any], Any]) -> List[AgentFact]:
    """The closed facts the memories `memories` (ids) ended in place
    (`agent_fact_ended_by`), on a query of AgentFact narrowed by `within`,
    read before those memories go."""
    E = AgentFactEndedBy
    return (within(db.query(AgentFact))
            .filter(AgentFact.tenant_id == tenant_id,
                    AgentFact.id.in_(db.query(E.fact_id).filter(E.tenant_id == tenant_id,
                                                                E.memory_id.in_(list(memories)))),
                    AgentFact.invalid_at.isnot(None))
            .all())


def _said_by_hand(db: Session, facts: List[AgentFact]) -> Set[UUID]:
    """(b): the ones of `facts` the caller said are over by hand, each with
    its record in agent_forget_audit (`record_by_hand`), whatever end the
    forget moved. The rows do not say it: a forget moves the end and leaves
    the pointer a closing gave it, and one that found the end already at its
    date moves nothing. One read for every fact of a deletion."""
    return _said_by_hand_ids(db, {f.tenant_id for f in facts}, [f.id for f in facts])


def _said_by_hand_at(db: Session, tenant_id: UUID, ids: Iterable[UUID]) -> Dict[UUID, datetime]:
    """For the ones of `ids` the caller said are over by hand (`_said_by_hand`),
    when they last said it: the latest record's `created_at`. One read."""
    from ..ids import fact_public_id

    A = AgentForgetAudit
    named = {fact_public_id(i): i for i in ids}
    if not named:
        return {}
    return {named[t]: at for t, at in (
        db.query(A.target_id, func.max(A.created_at))
        .filter(A.tenant_id == tenant_id, A.scope.in_(BY_HAND), A.target_id.in_(sorted(named)))
        .group_by(A.target_id).all())}


def _said_by_hand_ids(db: Session, tenants: Any, ids: Iterable[UUID]) -> Set[UUID]:
    """`_said_by_hand` for fact ids of the accounts `tenants` (one or a set)."""
    from ..ids import fact_public_id

    A = AgentForgetAudit
    named = {fact_public_id(i): i for i in ids}
    if not named:
        return set()
    tenants = tenants if isinstance(tenants, (set, frozenset, list, tuple)) else {tenants}
    told = (db.query(A.target_id)
            .filter(A.tenant_id.in_(list(tenants)), A.scope.in_(BY_HAND),
                    A.target_id.in_(sorted(named)))
            .distinct().limit(len(named)).all())
    return {named[t] for (t,) in told}


class Held:
    """What a write of `memory` holds over (memory_core 6.14.0, the founder's
    decision of 15 Nov 2026: a forget is the person's will, and an edit of
    the text does not undo it): the canonical triples the memory stated
    (any row of them) of which no row is true now, and whose last end by
    hand (a forget, a correction, a past statement posted by hand, the
    records of `record_by_hand`) is later than the last row of it the
    caller posted by hand (POST /v1/facts, a row born of no memory). The
    rows born of memories are not the caller's word either way: a memory
    that says the fact again, then is forgotten, erased or edited, leaves
    the caller's forget the last word (an earlier form read the triple's
    last row, and a memory forget of a row a
    later memory wrote let the edit undo the caller's). The fact posted
    again lifts the hold while it stands; forgotten again, the hold is back.

    Read once, before the write, as columns (memory_core
    `store.rows_of_the_triples_stated_by`, and the records): a forget
    another session committed meanwhile is read as committed. With no such
    triple (the common case) it costs those reads, and every statement of
    the write nothing more.

    Called with a statement's canonical triple (memory_core
    `writing.write_fact(held_by_hand=)`), the held row (the row of the last
    end by hand), read again from the database, or None. `ids` are the held
    rows in the order of their triple: PATCH /v1/memories/{id} answers them
    as `kept_forgotten`, the same before the write and after it."""

    def __init__(self, db: Session, memory: Any):
        from ..memory_core.store import rows_of_the_triples_stated_by
        from .entities import TABLES, memory_of, memory_scope

        self.db = db
        now = datetime.now(timezone.utc)
        scope = memory_scope(memory.tenant_id, memory.project_id, memory.end_user_id)
        # A correction of one of the memory's facts names no source memory
        # since 1.0.27, and keeps the memory as its first writer (made of its
        # words, `first_memory_id`): its triple is one the memory stated too,
        # so a correction of a correction stays the caller's last word
        # (without it, Paris corrected to Lyon, then to Nice, then the text
        # edited to say Lyon brought Lyon back).
        rows = rows_of_the_triples_stated_by(
            db, TABLES, memory_of(memory.tenant_id, memory.project_id, memory.end_user_id,
                                  memory.agent_id),
            memory.id, scope=scope, also=and_(scope, AgentFact.first_memory_id == memory.id))
        ended = _said_by_hand_at(db, memory.tenant_id, [r.id for r in rows])
        triples: Dict[Tuple[str, str, str], List[Any]] = {}
        for r in rows:
            triples.setdefault((r.s, r.p, r.o), []).append(r)
        self.rows: Dict[Tuple[str, str, str], UUID] = {}
        for key in sorted(triples):
            of_it = triples[key]
            if any(_true(r, now) for r in of_it):
                continue
            ends = [(ended[r.id], r.id) for r in of_it if r.id in ended]
            if not ends:
                continue
            last_end, row = max(ends, key=lambda e: (_aware(e[0]), str(e[1])))
            posted = [_aware(r.created_at) for r in of_it if not r.of_a_memory]
            if not posted or _aware(last_end) > max(posted):
                self.rows[key] = row
        self.held: Set[UUID] = set()

    @property
    def ids(self) -> List[UUID]:
        return list(self.rows.values())

    def __call__(self, subject: str, relation: str, obj: str) -> Optional[AgentFact]:
        if not self.rows:
            return None
        # The database's lower() on both sides, as memory_core `find_open`.
        s, o = self.db.query(func.lower((subject or "").strip()),
                             func.lower((obj or "").strip())).one()
        fact_id = self.rows.get((s, relation, o))
        if fact_id is None:
            return None
        row = (self.db.query(AgentFact).populate_existing()
               .filter(AgentFact.id == fact_id).one_or_none())
        if row is None or row.invalid_at is None:
            return None
        self.held.add(row.id)
        return row


def _true(row: Any, now: datetime) -> bool:
    """`row` (valid_from, invalid_at) true at `now`, as `temporal.true_at`."""
    begins = _aware(row.valid_from)
    ends = _aware(row.invalid_at)
    return (begins is None or begins <= now) and (ends is None or ends > now)


def _aware(v: Optional[datetime]) -> Optional[datetime]:
    if v is None:
        return None
    return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)


def standing_corrections(db: Session, memory: Any, scope: Any) -> Set[UUID]:
    """The facts of `memory` not ended that are a correction by hand of
    another (PATCH /v1/facts/{id}, `service.correct_fact`): the corrected
    fact points at it and its forget record was written by the transaction
    that ended it, the rule by which the agent notices call a closure
    "corrected" (facts/changes.a_correction). A correction keeps the
    corrected fact's memory as its source (memory_core 6.6.0, the words of
    its subject), and an edit of that memory leaves it standing, whatever
    the new text says (the decision of 15 Nov 2026: a correction is the
    person's will, as a forget is). A forget of a fact the judge had already
    closed is no correction: its record is of another transaction than the
    closing."""
    from sqlalchemy.orm import aliased

    from ..memory_core.store import source_condition
    from .changes import a_correction
    from .entities import TABLES
    from .temporal import not_ended

    mine = [r.id for r in db.query(AgentFact.id).filter(
        source_condition(TABLES, memory.id, scope=scope), not_ended())]
    if not mine:
        return set()
    closer = aliased(AgentFact, name="closer")
    return {r.closer for r in (
        db.query(closer.id.label("closer"))
        .join(AgentFact, AgentFact.invalidated_by_id == closer.id)
        .filter(closer.id.in_(mine), AgentFact.tenant_id == memory.tenant_id,
                a_correction(memory.tenant_id, closer)))}


def _standing(db: Session, facts: List[AgentFact]) -> Dict[UUID, Dict[UUID, Optional[datetime]]]:
    """(b) and `_begins`: for each of `facts`, the memories that ended it in
    place (`agent_fact_ended_by`), each with when it was forgotten (None:
    live, so its words still stand). One of another account than the
    fact's is not one of them. One read for every fact of a deletion."""
    if not facts:
        return {}
    E, M, F = AgentFactEndedBy, AgentMemory, AgentFact
    out: Dict[UUID, Dict[UUID, Optional[datetime]]] = {}
    for fact_id, memory_id, forgotten in (
            db.query(E.fact_id, M.id, M.deleted_at)
            .join(M, M.id == E.memory_id).join(F, F.id == E.fact_id)
            .filter(E.fact_id.in_([f.id for f in facts]), M.tenant_id == F.tenant_id).all()):
        out.setdefault(fact_id, {})[memory_id] = forgotten
    return out


def _retracted(db: Session, fact_id: UUID) -> bool:
    """(a): the fact a row points at was retracted by an earlier deletion: it
    is closed, and no live memory states it any more (its memories forgotten,
    or the edit of its memory that took it away, `agent_fact_ended_by`). One
    posted by hand (no memory) stands, and so does one closed by a newer
    value or said over by another memory while its own memory is live: it
    was true, and what it closed stays history."""
    from ..memory_core.store import sources_of
    from .entities import TABLES

    g = db.get(AgentFact, fact_id)
    if g is None:
        return True
    if g.invalid_at is None:
        return False
    ids = []
    for s in sources_of(TABLES, g):
        try:
            ids.append(UUID(str(s)))
        except ValueError:
            continue
    if not ids:
        return False
    E = AgentFactEndedBy
    live = db.query(AgentMemory.id).filter(AgentMemory.tenant_id == g.tenant_id,
                                           AgentMemory.id.in_(ids),
                                           AgentMemory.deleted_at.is_(None),
                                           ~AgentMemory.id.in_(db.query(E.memory_id)
                                                               .filter(E.fact_id == g.id)))
    return live.first() is None


def _vouching(db: Session, f: AgentFact) -> Tuple[List[str], List[str]]:
    """(b): F's live source memories (not forgotten, not erased), in F's
    order, split by when their current text arrived (`worker.arrival`: its
    last edit, else its creation) against when the store learned F had ended
    (`closed_known_at`, else `created_at`, as `temporal.end_as_known` reads
    it): (those that vouch, arrived by then; those that arrived since). Two
    clocks (Graphiti, Rasmussen et al. 2025, section 2.2.3), no model.

    A text read before the closing is one the judge weighed and found older
    than the closer. A live source whose text arrived since is later word on
    F, and F stays closed: the memory edited to say something else (its old
    facts were closed already, and the edit's retirement takes the open
    ones only, so F kept it), or "I used to live in Paris", which memory_core
    `writing._the_past` adds to the closed row as its history and which says
    F is over whatever closed it. A statement of F as true now after the
    closing is a new open row, not a source of the closed one, and (c) finds
    it."""
    from ...core.times import aware
    from ..memory_core.store import sources_of
    from ..memory_core.worker import arrival
    from .entities import TABLES

    sources = [s for s in sources_of(TABLES, f) if s]
    if not sources:
        return [], []
    ids = []
    for s in sources:
        try:
            ids.append(UUID(str(s)))
        except ValueError:
            continue
    learned = aware(f.closed_known_at or f.created_at)
    then = {str(m.id): arrival(m) is not None and arrival(m) <= learned
            for m in db.query(AgentMemory.id, AgentMemory.created_at, AgentMemory.updated_at)
            .filter(AgentMemory.tenant_id == f.tenant_id, AgentMemory.id.in_(ids),
                    AgentMemory.deleted_at.is_(None)).all()}
    return ([s for s in sources if then.get(str(s)) is True],
            [s for s in sources if then.get(str(s)) is False])


def _its_triple(f: AgentFact):
    """A row of F's own canonical triple, as the one-open-row index compares
    it (`uq_agent_facts_one_open_per_triple`: lowercased names) on the names
    F' holds (`_continuation`): a NULL name is read as its label (`_held`),
    on F and on the row, so neither side is ever NULL (`predicate_canonical`
    is NOT NULL). One condition for (c), which leaves the triple out, and
    (d), which looks for it. Read as the index reads it, NULL equal to NULL,
    F's copy in one agent given back with its label would not be F's
    triple for the copy with NULL in the next agent, and would keep it
    closed; negated, a plain `=` on a NULL would drop the row, and with it a
    value that stands."""
    F = AgentFact
    return and_(
        _held_in(F.subject_canonical, F.subject_label)
        == func.lower(_held(f.subject_canonical, f.subject_label)),
        F.predicate_canonical == f.predicate_canonical,
        _held_in(F.object_canonical, F.object_label)
        == func.lower(_held(f.object_canonical, f.object_label)),
    )


def one_triple(a, b):
    """SQL: the rows `a` and `b` (aliases of AgentFact) are of one triple, as
    the one-open-row index reads it (`uq_agent_facts_one_open_per_triple`):
    its scope (account, project, end user and agent, NULL equal to NULL) and
    the canonical triple, lowercased, a NULL name read as its label as
    `_its_triple` reads it. F' (`_continuation`) is such a row of F, and so
    is the same triple told again; nothing else links them."""
    return and_(
        a.tenant_id == b.tenant_id,
        a.project_id.is_not_distinct_from(b.project_id),
        a.end_user_id.is_not_distinct_from(b.end_user_id),
        a.agent_id.is_not_distinct_from(b.agent_id),
        _held_in(a.subject_canonical, a.subject_label)
        == _held_in(b.subject_canonical, b.subject_label),
        a.predicate_canonical == b.predicate_canonical,
        _held_in(a.object_canonical, a.object_label)
        == _held_in(b.object_canonical, b.object_label),
    )


def _told_since(db: Session, f: AgentFact, gone: Set[UUID], begins) -> bool:
    """(d): another row of F's own canonical triple, in the scope of the
    one-open-row index (account, project, end user and agent,
    `uq_agent_facts_one_open_per_triple`; lowercased names), other than the
    rows retracted now, that began at or after F's end, or is true at or
    after `begins`: the triple's later history is in the store already (F
    given back before, open or closed since by hand or by a newer value, or
    told again by another memory), and a second row from `begins` would be
    true beside it, or after an end somebody gave it."""
    from sqlalchemy import or_

    F = AgentFact
    return db.query(F.id).filter(
        F.tenant_id == f.tenant_id,
        F.project_id.is_not_distinct_from(f.project_id),
        F.end_user_id.is_not_distinct_from(f.end_user_id),
        F.agent_id.is_not_distinct_from(f.agent_id),
        _its_triple(f),
        ~F.id.in_(list(gone | {f.id})),
        or_(F.valid_from >= f.invalid_at, F.invalid_at.is_(None), F.invalid_at > begins),
    ).first() is not None


def _open_beside(db: Session, f: AgentFact, relations: Set[str], retracted: Set[UUID],
                 now: datetime) -> bool:
    """(c): an open fact (not ended: no end, or one ahead, `temporal.
    not_ended`) of F's memory about the same subject, in F's relation or in
    the relation of the fact that closed it, with another value than F's,
    other than the facts retracted now. F's own canonical triple, in any
    agent, is never another value: F told to two agents of the end user is a
    row in each agent's namespace, one closer closes both, and the copy
    given back to the first, or one told to the second again since, says
    what F says. Within F's agent a row of the triple is F's own later
    history, and (d) finds it (F given back already, or restated since:
    nothing to give back).

    The same value is the same canonical names, lowercased, as the
    open-triple index compares them. A name is resolved in its own
    agent's memory (`entities.memory_of`, an alias is a statement of one
    memory), so two agents that resolved one place to two names ("Paris" in
    one, "Parigi" in the other by an alias of its own) hold two
    values here, and a copy given back to one keeps the other closed: whether
    they are one place is the judge's question, and no judge is called on a
    deletion.

    Why this is enough without a judge: what reopens F is the deletion of
    the one statement the judge read as ending it. Another value standing
    now in the same relation of the same subject is the same kind of
    statement, and for a single-valued relation it is the contradiction
    itself. The relation's single-value label (`PredicateCluster.
    is_functional`) is NOT read to let F back beside it: the label is for the
    judge, never a rule that decides a fact alone (Lin, Mausam, Etzioni,
    EMNLP 2010 §3; Ritter et al., EMNLP 2008), and whether a value of a
    many-valued or undecided relation contradicts F is the judge's question.
    No judge is called on a deletion, so F stays closed: conservative there.
    It is not conservative for a contradiction under a relation that is
    neither F's nor its closer's ("relocated_to Rome" beside "lives_in
    Paris"): the judge would see it, this does not, and F comes back beside
    it, as a write of F would have before the judge."""
    from .entities import memory_scope
    from .temporal import not_ended

    F = AgentFact
    return db.query(F.id).filter(
        memory_scope(f.tenant_id, f.project_id, f.end_user_id),
        not_ended(now),
        _held_in(F.subject_canonical, F.subject_label)
        == func.lower(_held(f.subject_canonical, f.subject_label)),
        F.predicate_canonical.in_(sorted(relations | {f.predicate_canonical})),
        ~_its_triple(f),
        ~F.id.in_(list(retracted)),
    ).first() is not None


def give_back(db: Session, closed: Closed, *, retracted: Iterable[UUID] = (),
              erased: Iterable[UUID] = ()) -> List[AgentFact]:
    """Each fact of `closed` that (a), (b), (c) and (d) let back, as a new open
    row (the module's docstring). `retracted`: the facts retracted now that
    are still in the store (forgotten, retired, or about to be erased in
    this transaction): none of them is given back or counts as open.
    `erased`: those of them about to be deleted, which never were (`_begins`);
    a row already deleted is not found and counts the same. Not committed;
    returns the rows written."""
    if not closed.facts:
        return []
    from ...core.times import aware

    gone = set(retracted) | closed.through
    now = datetime.now(timezone.utc)
    db.flush()
    rows = (db.query(AgentFact).filter(AgentFact.id.in_(list(closed.facts)))
              .order_by(AgentFact.valid_from, AgentFact.id)
              .populate_existing().all())
    out: List[AgentFact] = []
    by_hand = _said_by_hand(db, rows)
    words = _standing(db, rows)
    # The rows the words retracted now ended, and a row a past statement's
    # judgement closed, which points at the row the statement landed on:
    # it follows that row (a), so it is weighed after it.
    said = {i for i, c in closed.facts.items() if c.words}
    rows.sort(key=lambda r: r.invalidated_by_id in closed.facts)
    given: Dict[UUID, UUID] = {}
    for f in rows:
        if f.id in gone or aware(f.invalid_at) is None:
            continue
        # (a): what closed it is what was retracted now, a row that went, a
        # fact an earlier deletion retracted, which then counts as gone, or
        # a row the same words ended, given back by now with its copy.
        lost: Set[UUID] = set()
        pointer = f.invalidated_by_id
        if pointer is not None and pointer not in gone:
            if pointer in said:
                if pointer not in given:
                    continue
                lost = {pointer, given[pointer]}
            elif not _retracted(db, pointer):
                continue
            else:
                lost = {pointer}
        # (b): the caller said it is over, or a memory that ended it in place
        # and is not retracted now is live.
        others = {m: t for m, t in words.get(f.id, {}).items() if m not in closed.words_of}
        if f.id in by_hand or any(t is None for t in others.values()):
            continue
        live, since = _vouching(db, f)
        if since or (not live and f.source_memory_id is not None):
            continue
        if _open_beside(db, f, closed.facts[f.id].relations, gone | lost, now):   # (c)
            continue
        begins = _begins(db, f, closed.facts[f.id], set(erased),
                         max((aware(t) for t in others.values()), default=None))
        if _told_since(db, f, gone | lost, begins):                   # (d)
            continue
        new = _continuation(f, live, begins)
        try:
            # Its own savepoint: a write of the same triple committed
            # meanwhile holds the one open row of it
            # (`uq_agent_facts_one_open_per_triple`), and the triple is then
            # true already.
            with db.begin_nested():
                db.add(new)
                db.flush()
        except IntegrityError:
            # The savepoint's rollback takes the row out of the session; if
            # it is still there, out by hand, or the next flush tries it
            # again (memory_core `writing._insert` does the same).
            if new in db:
                db.expunge(new)
            continue
        out.append(new)
        given[f.id] = new.id
    if out:
        from .erasure import recount_session_only

        recount_session_only(db, out)
        db.flush()
    return out


def _begins(db: Session, f: AgentFact, closer: Closer, erased: Set[UUID],
            forgotten: Optional[datetime] = None):
    """Where F' begins: the later of F's end and the ends of what closed it
    that the store still holds (`Closer.chain`), as their rows are now; an
    erased one (about to go, `erased`, or gone already) never was and has no
    end. And no earlier than the memories that ended F in place stopped
    saying it: retracted now, until `Closer.until` (its forget, or the
    arrival of the text that no longer says it; erased, never); forgotten
    before, `forgotten`, the latest of their forgets. Never before F's end,
    so F and F' are never true together."""
    from ...core.times import aware

    kept = [i for i in closer.chain if i not in erased]
    ends = [aware(e) for (e,) in db.query(AgentFact.invalid_at)
            .filter(AgentFact.id.in_(kept), AgentFact.invalid_at.isnot(None)).all()] if kept else []
    said = [aware(t) for t in (closer.until if closer.words else None, forgotten) if t is not None]
    return max([aware(f.invalid_at)] + ends + said)


def _continuation(f: AgentFact, live: List[str], begins) -> AgentFact:
    """F' (the module's docstring): F's statement, from `begins`, stated by
    the sources that vouch for it, or by none when F was stated by hand (its
    restatements counted as F's). Its vector is F's (the same text), and the
    judgement it carries is F's: nothing new was said, and no judge is
    called on a deletion. `created_at` is now (the database's), when the
    store learned Paris was true again; `first_memory_id` the database sets
    from `source_memory_id` (db/triggers.py), or F's when no source vouches
    for it: a correction given back is still made of the words of the memory
    whose erasure takes it (facts/erasure._made_of_its_words)."""
    from ..vector_state import DONE, PENDING, STALE

    emb = f.embedding
    has_vector = emb is not None and f.embedding_status in (DONE, STALE)
    return AgentFact(
        tenant_id=f.tenant_id, project_id=f.project_id,
        end_user_id=f.end_user_id, agent_id=f.agent_id, run_id=f.run_id,
        subject_label=f.subject_label, subject_type=f.subject_type,
        # The names as (c) and (d) compared them (`_its_triple`).
        subject_canonical=_held(f.subject_canonical, f.subject_label),
        predicate_raw=f.predicate_raw, predicate_canonical=f.predicate_canonical,
        predicate_family=f.predicate_family,
        object_label=f.object_label, object_canonical=_held(f.object_canonical, f.object_label),
        object_is_literal=f.object_is_literal,
        # An open row is not history: a fact a past statement closed in place
        # carries tense 'past' (memory_core `writing._the_past`), and what is
        # given back is true now (E3, memory_core 6.3.0).
        confidence=f.confidence, model_used=f.model_used,
        tense="current" if f.tense == "past" else f.tense,
        valid_from=begins, invalid_at=None,
        # With no memory, as the engine writes a fact posted by hand
        # (memory_core `writing._insert`: no source, `source_memory_ids` None).
        source_memory_id=UUID(str(live[0])) if live else None,
        first_memory_id=None if live else f.first_memory_id,
        source_memory_ids=list(live) if live else None,
        observation_count=len(live) if live else int(f.observation_count or 1),
        last_confirmed_at=f.last_confirmed_at,
        embedding=emb if has_vector else None,
        embedding_status=f.embedding_status if has_vector else PENDING,
        judged_before_vector=bool(f.judged_before_vector) or has_vector,
    )
