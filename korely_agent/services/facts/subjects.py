"""Two subjects of a memory with no end user that are one thing: the product's half
(memory_core 6.20.0 `objects.review_subjects`, off by default).

A team's memory wrote one product under four names ("Cloud di Korely",
"Cloud", "Korely Cloud", "Il Cloud di Korely"), and with no end user the
judge weighs a fact only against the facts of its own subject (memory_core
6.9.0): the version facts of one name stayed open while another name moved
on. `manage review-subjects` asks, once for each pair, whether two subjects
of the judge's wall (`entities.wall_of`: the account, the project, no end
user, every agent) are one thing; a "same" twice is a LINK of scope
`entities.WALL_LINKS` (`agent_object_links`), never an alias: no fact is
rewritten, and the reads do not follow it.

What follows from a link, with the switch on (`SAME_SUBJECT_REVIEW`):

  * the judge of a write with no end user weighs the new fact also against
    the facts of the other names of its subject's group (`extra_candidates`,
    memory_core `candidates.for_the_judge(subject_names=)`), and every
    closing it makes only so is recorded on the links of the path
    (`record`, `agent_fact_closure_links`);
  * after `apply`, the open facts of the names just united are judged
    against the earlier facts of the other names, the judgement their
    writes would have made had the link existed (`rejudge`), fact by fact,
    each in its own transaction;
  * the one door a link leaves by (`remove`, memory_core
    `objects.remove_link`) gives back first every closing recorded on it
    (facts/given_back.py: a new open row from where the fact was closed,
    the closed row kept as the store knew it), whoever removes it: the
    dashboard (DELETE /admin/object-links/{id}), a project's move, and an
    erasure (of a memory, of an agent) that leaves one of its two names
    with no open fact (`after_the_facts_went`). Forgetting a memory never
    touches it, and an erasure points it at another memory of the name
    (`before_the_memories_go`).

The model is the decisions model of the project's region
(`core/llm.decisions_provider`: OpenRouter's decisions API, Global only);
a European project is never asked, and nothing falls back to Global. A dry
run calls no model. A provider that does not answer (down, 402, 429) stops
the review (`deferred`): nothing is written and the next run asks again; no
write ever waits for it, and no lock is held while the model is asked.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from uuid import UUID

from sqlalchemy import func, or_, text as _sql
from sqlalchemy.orm import Session

from ...db.models import AgentFact, AgentMemory, AgentObjectLink
from ..memory_core import objects as _objects
from ..memory_core.identity import MAX_HOPS

# The switch (a module-level constant the tests flip, never an environment
# variable: services/context_experiments.py says why). Off: the judge reads
# no link of a wall, and `manage review-subjects` refuses to ask a model
# without --while-off; its dry run, which calls no model, always runs. A
# switch off by default goes on only after a measurement and the founder's
# yes (CONTRIBUTING.md); the design and its limits are told in this module's
# docstring, in the constants below and in memory_core `objects.review_subjects`.
SAME_SUBJECT_REVIEW = False

#: How many links of one wall the judge reads: at most one per subject name.
WALL_LINKS_MAX = 5000
#: How many open facts of a group one `rejudge` weighs, oldest first.
REJUDGE_MAX = 2000


class RegionRefused(RuntimeError):
    """The project's region has no decisions model (a European project, or
    no OPENROUTER_API_KEY): nothing is asked, never of another region."""


# ---------------------------------------------------------------------------
# the wall's links
# ---------------------------------------------------------------------------

def wall_links(db: Session, tenant_id: UUID, project_id: Optional[UUID]) -> list:
    """The links of the wall (`entities.wall_of`), bounded."""
    from .entities import wall_of

    L = AgentObjectLink
    return (db.query(L).filter(wall_of(tenant_id, project_id).links)
              .order_by(L.created_at, L.id).limit(WALL_LINKS_MAX).all())


def _path(name: Optional[str], by_key: Dict[str, Any]) -> List[Any]:
    """The ids of the links followed from `name` to its group's root, at
    most `identity.MAX_HOPS` (the walk of `objects.root_of`)."""
    out: List[Any] = []
    key = _objects.key_of(name)
    for _ in range(MAX_HOPS):
        row = by_key.get(key)
        if row is None or row.id in out:
            break
        out.append(row.id)
        key = _objects.key_of(row.canonical_label)
    return out


def extra_candidates(db: Session, tenant_id: UUID, project_id: Optional[UUID], end_user_id: Optional[str],
                     subject_label: str, subject_canonical: Optional[str], base: Sequence[Any],
                     gather: Callable[[List[str]], List[Any]], *,
                     on: Optional[bool] = None) -> Tuple[List[Any], Dict[Any, List[Any]]]:
    """The judge's candidates that enter only through the group of the new
    fact's subject, and for each the links of the path that let it in: the
    new fact's name to the root, and the candidate's subject (or object, the
    other role of `candidates.about_the_subject`) to the root. `base`: the
    candidates without the group (what the judge reads with the switch off);
    `gather(names)`: the candidates with it. With the switch off (`on`
    None reads `SAME_SUBJECT_REVIEW`), an end user, no link or a group of
    one name: ([], {}), and no statement is run but the read of the links
    (none with the switch off)."""
    if not (SAME_SUBJECT_REVIEW if on is None else on) or end_user_id is not None:
        return [], {}
    rows = wall_links(db, tenant_id, project_id)
    if not rows:
        return [], {}
    links = _objects.link_map(rows)
    name = subject_canonical or subject_label
    names = _objects.same_names(name, links)
    if len(names) < 2:
        return [], {}
    seen = {c.id for c in base}
    extra = [c for c in gather(sorted(names)) if c.id not in seen]
    by_key = {_objects.key_of(r.name_label): r for r in rows}
    own = _path(name, by_key)
    through: Dict[Any, List[Any]] = {}
    for c in extra:
        theirs = c.subject_canonical or c.subject_label
        if _objects.key_of(theirs) not in names:
            theirs = c.object_canonical or c.object_label
        through[c.id] = list(dict.fromkeys(own + _path(theirs, by_key)))
    return extra, through


def record(db: Session, new_fact_id: Any, closed: Sequence[Any], through: Dict[Any, List[Any]]) -> int:
    """The closings of one judgement made through a link (`extra_candidates`),
    recorded on the links of their path (memory_core `objects.
    record_closures`): an earlier fact the new one closed, or the new one,
    closed by a candidate that began later (memory_core `judge.close`)."""
    from .entities import TABLES

    if not through:
        return 0
    out: Dict[Any, List[Any]] = {i: through[i] for i in closed if i in through}
    new = db.get(AgentFact, new_fact_id) if new_fact_id is not None else None
    if new is None:
        return 0
    if new.invalid_at is not None and new.invalidated_by_id in through:
        out[new.id] = through[new.invalidated_by_id]
    return _objects.record_closures(db, TABLES, out, dict(tenant_id=new.tenant_id))


# ---------------------------------------------------------------------------
# the one door a link leaves by
# ---------------------------------------------------------------------------

def give_back(db: Session, fact_ids: Sequence[Any]) -> int:
    """Each fact of `fact_ids`, closed through a link that goes now, given
    back as facts/given_back.py gives back a fact whose closer no longer
    stands: its closer counts as gone for that fact alone ((a), and never
    as an end of its own, `_begins`: the closing never was), and the rules
    (b), (c) and (d) decide, with no model. One fact at a time, in the
    order they began, so a closer that is itself given back here is not
    read as gone for the facts it closed. Not committed; returns how many."""
    from .given_back import Closed, Closer, give_back as _give_back

    ids = list(dict.fromkeys(fact_ids))
    facts = (db.query(AgentFact).filter(AgentFact.id.in_(ids))
               .order_by(AgentFact.valid_from, AgentFact.id).limit(len(ids)).all()) if ids else []
    closers = list({f.invalidated_by_id for f in facts if f.invalidated_by_id is not None})
    relation = dict(db.query(AgentFact.id, AgentFact.predicate_canonical)
                    .filter(AgentFact.id.in_(closers)).limit(len(closers)).all()) if closers else {}
    n = 0
    for f in facts:
        g = f.invalidated_by_id
        if f.invalid_at is None or g is None:
            continue
        closed = Closed(facts={f.id: Closer(relations={relation[g]} if g in relation else set(), chain={g})},
                        through={g})
        n += len(_give_back(db, closed, erased=[g]))
    return n


def remove(db: Session, row: Any) -> int:
    """Remove a link, any link, by the one door (memory_core
    `objects.remove_link`): the closings recorded on it given back first.
    Not committed; returns how many facts were given back."""
    from .entities import TABLES

    return _objects.remove_link(db, TABLES, row, give_back=lambda ids: give_back(db, ids))


def remove_where(db: Session, condition) -> int:
    """Every link `condition` selects, each by the door (`remove`): a
    memory's forget and erasure, an agent's deletion. Read `_REMOVE_CHUNK`
    at a time, newest first, until none is left. Returns how many links
    went."""
    n = 0
    while True:
        rows = (db.query(AgentObjectLink).filter(condition)
                  .order_by(AgentObjectLink.created_at.desc(), AgentObjectLink.id).limit(_REMOVE_CHUNK).all())
        if not rows:
            return n
        for row in rows:
            remove(db, row)
        n += len(rows)


#: How many links `remove_where` reads at a time.
_REMOVE_CHUNK = 500


# ---------------------------------------------------------------------------
# a wall's links when memories are erased
# ---------------------------------------------------------------------------
#
# A link between two subjects is a statement about two names of the wall, not
# about the memory whose fact happened to be first: forgetting that memory
# leaves the link (its reason holds the two readings, no words), and erasing
# it, or deleting its agent, points the link at another live memory that
# still states a fact of the same name, or at none. The link leaves, by the
# door, only when one of its two names has no fact left in the wall, open or
# closed: there is nothing left for it to explain. Not when the name has no
# OPEN fact: its old facts may be closed through the link, and taking the
# link then would give them back, the open old version beside the new one
# that the link was made to close (the review of 23 Nov 2026, P5: a note on
# Caddy forgotten reopened the old version of the Cloud).

def _named_in_the_wall(db: Session, row: Any, name: str, *, open_only: bool = False):
    """A query of the facts of the wall of `row` whose subject is `name`
    (the judge's comparison, the database's lower()), the open ones only
    with `open_only`."""
    from .entities import memory_scope
    from .temporal import true_at

    F = AgentFact
    q = db.query(F).filter(memory_scope(row.tenant_id, row.project_id, None),
                           _objects.subject_key(F) == func.lower(func.trim(name)))
    return q.filter(true_at()) if open_only else q


def before_the_memories_go(db: Session, tenant_id: UUID, memories: Any, *, facts: Any = None) -> List[Any]:
    """Before `memories` (ids, or a select of ids) and `facts` (the facts
    that go with them, the same) are erased: every link of a wall of the
    account whose `source_memory_id` or `canonical_memory_id` is one of them
    points at another live memory stating a fact of its name that stays (an
    open one first), or at none (NULL), so that no cascade of the memory takes it past
    the door. Returns the ids of the links of the walls the erasure touches:
    those, and those that name a subject of the facts that go, for
    `after_the_facts_went`. Not committed."""
    from .entities import WALL_LINKS

    L, F, M = AgentObjectLink, AgentFact, AgentMemory
    rows = (db.query(L).filter(L.tenant_id == tenant_id, L.scope == WALL_LINKS,
                               or_(L.source_memory_id.in_(memories), L.canonical_memory_id.in_(memories)))
              .order_by(L.created_at, L.id).limit(WALL_LINKS_MAX).all())

    def another(row, name):
        from .temporal import true_at

        q = (_named_in_the_wall(db, row, name).join(M, M.id == F.source_memory_id)
             .filter(M.tenant_id == tenant_id, M.deleted_at.is_(None), ~F.source_memory_id.in_(memories)))
        if facts is not None:
            q = q.filter(~F.id.in_(facts))
        return (q.with_entities(F.source_memory_id)
                 .order_by(true_at().desc(), F.created_at, F.id).limit(1).scalar())

    for r in rows:
        ids = {i for (i,) in db.query(M.id).filter(
            M.tenant_id == tenant_id, M.id.in_([r.source_memory_id, r.canonical_memory_id]),
            M.id.in_(memories)).limit(2).all()}
        if r.source_memory_id in ids:
            r.source_memory_id = another(r, r.name_label)
        if r.canonical_memory_id in ids:
            r.canonical_memory_id = another(r, r.canonical_label)
    touched = [r.id for r in rows]
    if facts is not None:
        names = (db.query(F.project_id, _objects.subject_key(F))
                   .filter(F.tenant_id == tenant_id, F.end_user_id.is_(None), F.id.in_(facts))
                   .distinct().limit(WALL_LINKS_MAX).all())
        for project_id, key in names:
            touched += [i for (i,) in db.query(L.id).filter(
                L.tenant_id == tenant_id, L.scope == WALL_LINKS, L.project_id.is_not_distinct_from(project_id),
                or_(func.lower(L.name_label) == key, func.lower(L.canonical_label) == key))
                .limit(WALL_LINKS_MAX).all()]
    db.flush()
    return list(dict.fromkeys(touched))


def after_the_facts_went(db: Session, link_ids: Sequence[Any]) -> int:
    """After an erasure: each link of `link_ids` (`before_the_memories_go`)
    one of whose names has no fact left in its wall, open or closed, leaves
    by the door (`remove`), which gives back what is still recorded on it.
    Returns how many facts were given back. Not committed."""
    n = 0
    ids = list(link_ids)
    rows = db.query(AgentObjectLink).filter(AgentObjectLink.id.in_(ids)).limit(len(ids)).all() if ids else []
    for r in rows:
        if any(_named_in_the_wall(db, r, name).first() is None for name in (r.name_label, r.canonical_label)):
            n += remove(db, r)
    return n


# ---------------------------------------------------------------------------
# manage review-subjects
# ---------------------------------------------------------------------------

def walls_with_subjects(db: Session, tenant_id: UUID, *, project_id: Any) -> List[Optional[UUID]]:
    """The projects of the account (behind `project_id`) with facts of no end
    user: the walls `review` can read. One grouped read."""
    from ..sentinels import within_project

    q = (db.query(AgentFact.project_id)
           .filter(AgentFact.tenant_id == tenant_id, AgentFact.end_user_id.is_(None),
                   AgentFact.embedding.isnot(None)))
    q = within_project(q, AgentFact.project_id, project_id)
    return [r[0] for r in q.group_by(AgentFact.project_id).limit(10_000).all()]


def review(db: Session, tenant_id: UUID, project_id: Optional[UUID], *, dry_run: bool,
           before_each: Optional[Callable[[], bool]] = None) -> dict:
    """`manage review-subjects` for one wall (memory_core
    `objects.review_subjects`). `dry_run`: no model, the pairs that would be
    asked. Otherwise the decisions model of the project's region
    (`core/llm.decisions_provider`): `RegionRefused` when it has none (a
    European project, or no key), and nothing is asked of another region.
    Writes nothing: the result's `proposals` are what `apply` writes."""
    from ...core import llm
    from .. import regions
    from .entities import TABLES, wall_of
    from .objects import words_of
    from .temporal import true_at

    common = dict(words_of=lambda ids: words_of(db, tenant_id, ids), is_open=true_at())
    mem = wall_of(tenant_id, project_id)
    if dry_run:
        return _objects.review_subjects(db, TABLES, mem, dry_run=True, **common)
    region = regions.of_project(db, tenant_id, project_id)
    port = llm.decisions_provider(region)
    if port is None:
        raise RegionRefused(f"no decisions model for this project's region ({region or 'none'}): "
                            "the review asks only in the Global region, with OPENROUTER_API_KEY")
    # Every read is made before the first question, and the run writes
    # nothing: the transaction ends before each question (`release`), and no
    # lock of it, not even a table's ACCESS SHARE, is held while the model
    # answers.
    return _objects.review_subjects(db, TABLES, mem, choose=port.choose, model=port.model,
                                    before_each=before_each, release=db.rollback, **common)


def proposal_record(tenant_id: UUID, project_id, p: _objects.Proposal) -> dict:
    """A proposal as the run's file keeps it: the wall, the two names, the
    ids, the reason. The file holds the memory's names and facts: the
    operator's, to read and then delete."""
    s = lambda v: str(v) if v is not None else None  # noqa: E731
    return {"account_id": str(tenant_id), "project_id": s(project_id), "name": p.name,
            "same_as": p.canonical, "source_memory_id": s(p.source_memory_id),
            "source_fact_id": s(p.source_fact_id), "canonical_memory_id": s(p.canonical_memory_id),
            "reason": p.reason}


def lock_of(db: Session, tenant_id: UUID, project_id) -> Callable[[], None]:
    """The lock of one wall's links, held to the transaction's end: two
    applies of one wall write one after the other. Never held while a model
    is asked: only `apply` takes it, and asks none."""
    key = f"subject-links:{tenant_id}:{project_id}"

    def lock() -> None:
        db.execute(_sql("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
    return lock


def _of_the_wall(db: Session, tenant_id: UUID, project_id, r: dict) -> bool:
    """Whether the memories and the fact a record names are of the wall it
    is applied in (the account, the project, no end user): a file is the
    operator's, written or edited by hand, and a link must never point into
    another account or project (the review of 23 Nov 2026, P6: a foreign
    key into account B, and B's erasure of its own memory then failed on
    A's link)."""
    def uuid_or_none(v):
        return UUID(v) if v else None

    F, M = AgentFact, AgentMemory
    for key, model in (("source_memory_id", M), ("canonical_memory_id", M), ("source_fact_id", F)):
        rid = uuid_or_none(r.get(key))
        if rid is None:
            continue
        if db.query(model.id).filter(model.id == rid, model.tenant_id == tenant_id,
                                     model.project_id.is_not_distinct_from(project_id),
                                     model.end_user_id.is_(None)).first() is None:
            return False
    return True


def apply(db: Session, records: Sequence[dict]) -> List[dict]:
    """Write exactly the links of a run's `records` (`proposal_record`),
    each in its wall under the wall's lock, while both names are still the
    subject of an open fact there (memory_core `objects.apply_proposals`);
    no model is called. A record that names a memory or a fact outside its
    wall is "refused" (`_of_the_wall`). Returns one result per record
    ({"record", "outcome", "link_id"}). The caller commits (the lock goes
    with the commit), then runs `rejudge` on the results."""
    from .entities import TABLES, wall_of
    from .temporal import true_at

    def uuid_or_none(v):
        return UUID(v) if v else None

    out = []
    locked: Set[Tuple[Any, Any]] = set()
    for r in records:
        tenant_id, project_id = UUID(r["account_id"]), uuid_or_none(r.get("project_id"))
        if (tenant_id, project_id) not in locked:
            lock_of(db, tenant_id, project_id)()
            locked.add((tenant_id, project_id))
        if not _of_the_wall(db, tenant_id, project_id, r):
            out.append({"record": r, "outcome": "refused", "link_id": None})
            continue
        p = _objects.Proposal(name=r["name"], canonical=r["same_as"],
                              source_memory_id=uuid_or_none(r.get("source_memory_id")),
                              source_fact_id=uuid_or_none(r.get("source_fact_id")),
                              canonical_memory_id=uuid_or_none(r.get("canonical_memory_id")),
                              reason=r.get("reason") or {}, family=_objects.SUBJECT_FAMILY)
        [(_p, row, outcome)] = _objects.apply_proposals(
            db, TABLES, wall_of(tenant_id, project_id), [p], is_open=true_at(),
            names_of=_objects.subject_key, decided_by=_objects.BY_A_SUBJECT_REVIEW)
        out.append({"record": r, "outcome": outcome, "link_id": str(row.id) if row is not None else None})
    return out


def group_of(db: Session, tenant_id: UUID, project_id: Optional[UUID], name: str) -> List[str]:
    """The names of `name`'s group in the wall, its own included, sorted
    (the keys `objects.same_names` compares)."""
    return sorted(_objects.same_names(name, _objects.link_map(wall_links(db, tenant_id, project_id))))


def rejudge(db: Session, tenant_id: UUID, project_id: Optional[UUID], name: str, *,
            while_off: bool = False) -> dict:
    """The judgements a link makes possible (the late judgement of
    memory_core `judge.after_the_vector`, for a group instead of a vector):
    each open current fact of `name`'s group, oldest first, against the
    earlier facts its write could not weigh, those of the other names of
    the group (`extra_candidates`), by the judge of the project's region, in
    one call per fact that has any, each fact committed on its own; the
    closings are recorded on the links. A judge that gives no answer stops
    it (`deferred`), its fact rolled back alone: `apply` of the same file
    runs it again. `while_off`: run although the switch is off (`manage
    review-subjects --while-off`). Returns {"facts", "judged", "closed",
    "deferred"}."""
    from .. import regions
    from .contradiction import JudgeUnavailable, JudgeUnreadable, judge_through_the_group
    from .temporal import true_at

    out = {"facts": 0, "judged": 0, "closed": 0, "deferred": None}
    on = SAME_SUBJECT_REVIEW or while_off
    if not on:
        return out
    names = group_of(db, tenant_id, project_id, name)
    if len(names) < 2:
        return out
    F = AgentFact
    from .entities import memory_scope

    ids = [i for (i,) in db.query(F.id).filter(
        memory_scope(tenant_id, project_id, None), true_at(), F.tense == "current",
        _objects.subject_key(F).in_(names)).order_by(F.created_at, F.id).limit(REJUDGE_MAX).all()]
    out["facts"] = len(ids)
    with regions.writing_for(db, tenant_id, project_id):
        for fid in ids:
            y = db.query(F).filter(F.id == fid, true_at()).first()
            if y is None:
                continue    # closed meanwhile, by this rejudge or a write
            try:
                recorded = judge_through_the_group(db, tenant_id, project_id, y, on=on)
                if recorded is None:
                    continue
                out["judged"] += 1
                out["closed"] += recorded
                db.commit()
            except (JudgeUnavailable, JudgeUnreadable) as e:
                from ..memory_core import log_safe

                db.rollback()
                out["deferred"] = log_safe.error(e)
                break
    return out
