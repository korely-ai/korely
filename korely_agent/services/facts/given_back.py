"""What a retracted fact had closed, given back (the founder, 31 Oct 2026).

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
    key's SET NULL when the closer was erased);
(b) something still vouches for F, and nothing live has said otherwise
    since: a source memory that is live and whose current text was there
    before the store learned F had ended, or F stated by hand (POST
    /v1/facts: no source memory, the developer's statement stands); and no
    live source whose text arrived after (`_vouching`). F' is stated by the
    vouching memories only;
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
Only a fact closed by a newer fact is given back: one closed in place by a
forget of the fact itself names no closer. A past statement names, since
memory_core 6.3.0, the fact of its own write that replaced the value ("moved
to Pro, it used to be Basic"): forgotten, it gives Basic back if another live
memory still says it, with tense 'current'. An end still
ahead (a closer dated ahead, forgotten before its date) is given back from
there, where F ends (`_begins`), so the two are never true together.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Set, Tuple
from uuid import UUID

from sqlalchemy import and_, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...db.models import AgentFact, AgentMemory

# How far down a chain of continuations a closer is followed: a chain is a
# few links (`service._current_successor` takes the same bound), and a loop
# is a corrupt store, not a reason to hang a deletion.
_CHAIN = 50


@dataclass
class Closer:
    """What closed one fact: the relations of its closer (for (c)), and the
    ids from the closer down to the fact retracted now (`chain`: the closer
    alone, or a row of its triple continued by the retracted one), whose
    ends say where the fact begins again (`_begins`)."""

    relations: Set[str] = field(default_factory=set)
    chain: Set[UUID] = field(default_factory=set)


@dataclass
class Closed:
    """The facts the retracted ones closed, read before the retracted rows
    go (an erased fact's closings lose their `invalidated_by_id` by the
    foreign key): `facts` {closed fact id: its Closer}, `through` every id
    that counts as "no longer stands" for (a)."""

    facts: Dict[UUID, Closer] = field(default_factory=dict)
    through: Set[UUID] = field(default_factory=set)


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
                     within: Callable[[Any], Any]) -> Closed:
    """The closed facts whose closer is one of `retracted` (fact rows, ids,
    or a select of ids), on a query of AgentFact narrowed by `within` (the
    retracted facts' memory, `entities.memory_scope`, or the erasure's
    project, `sentinels.within_project`). A row of the same
    triple a retracted fact continues (a plan that came true) is followed to
    what IT closed, and is not itself given back. Read only; call it before
    the retracted rows are deleted."""
    if isinstance(retracted, (list, tuple, set, frozenset)):
        ids = {r.id if isinstance(r, AgentFact) else r for r in retracted}
        if not ids:
            return Closed()
        frontier: Any = list(ids)
    else:
        ids, frontier = set(), retracted
    db.flush()
    out = Closed(through=set(ids))
    closers: Dict[UUID, AgentFact] = {}
    below: Dict[UUID, Set[UUID]] = {}   # a followed row: it and what continues it
    for _ in range(_CHAIN):
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
    for i in out.through:
        out.facts.pop(i, None)
    return out


def after_retracting(db: Session, tenant_id: UUID, retracted: List[AgentFact], scope: Any,
                     *, erased: bool = False) -> int:
    """What the facts `retracted` now (forgotten, erased or retired with the
    last memory that stated them) had closed, given back where nothing else
    closes it; `scope` the memory's (`entities.memory_scope`), where the
    judge's candidates were. `erased`: the retracted rows are deleted in this
    transaction, after this. Not committed; returns how many."""
    if not retracted:
        return 0
    closed = what_they_closed(db, tenant_id, retracted, within=lambda q: q.filter(scope))
    ids = [f.id for f in retracted]
    return len(give_back(db, closed, retracted=ids, erased=ids if erased else ()))


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
    for f in rows:
        if f.id in gone or aware(f.invalid_at) is None:
            continue
        # (a): what closed it is what was retracted now, or a row that went.
        if f.invalidated_by_id is not None and f.invalidated_by_id not in gone:
            continue
        live, since = _vouching(db, f)                                # (b)
        if since or (not live and f.source_memory_id is not None):
            continue
        if _open_beside(db, f, closed.facts[f.id].relations, gone, now):   # (c)
            continue
        begins = _begins(db, f, closed.facts[f.id].chain, set(erased))
        if _told_since(db, f, gone, begins):                          # (d)
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
    if out:
        from .erasure import recount_session_only

        recount_session_only(db, out)
        db.flush()
    return out


def _begins(db: Session, f: AgentFact, chain: Set[UUID], erased: Set[UUID]):
    """Where F' begins: the later of F's end and the ends of what closed it
    that the store still holds (`Closer.chain`), as their rows are now; an
    erased one (about to go, `erased`, or gone already) never was and has no
    end. Never before F's end, so F and F' are never true together."""
    from ...core.times import aware

    kept = [i for i in chain if i not in erased]
    ends = [aware(e) for (e,) in db.query(AgentFact.invalid_at)
            .filter(AgentFact.id.in_(kept), AgentFact.invalid_at.isnot(None)).all()] if kept else []
    return max([aware(f.invalid_at)] + ends)


def _continuation(f: AgentFact, live: List[str], begins) -> AgentFact:
    """F' (the module's docstring): F's statement, from `begins`, stated by
    the sources that vouch for it, or by none when F was stated by hand (its
    restatements counted as F's). Its vector is F's (the same text), and the
    judgement it carries is F's: nothing new was said, and no judge is
    called on a deletion. `created_at` is now (the database's), when the
    store learned Paris was true again; `first_memory_id` the database sets
    from `source_memory_id` (db/triggers.py)."""
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
        source_memory_ids=list(live) if live else None,
        observation_count=len(live) if live else int(f.observation_count or 1),
        last_confirmed_at=f.last_confirmed_at,
        embedding=emb if has_vector else None,
        embedding_status=f.embedding_status if has_vector else PENDING,
        judged_before_vector=bool(f.judged_before_vector) or has_vector,
    )
