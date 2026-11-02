"""The database half of the shared rules: aliases, reconfirmation, merging, vocabulary (memory_core).

Everything here works on the fact table the engine passes, korely-agent's
`agent_facts` (until 11 Oct 2026 also the fact tables of an earlier product
that shared the engine): its model classes (`Tables`) and the SQL condition that
selects ONE memory (`Memory`); the rules are written once.

Dependency: SQLAlchemy 2 only (korely-agent pins `sqlalchemy>=2,<3`).
Nothing from the application is imported here.

What a "memory" is: tenant + project + end user + agent namespace (the same
code is the Self-hosted and the Cloud since 9 Oct 2026). Until 4.0.0 the
personal-memory engine's memory, one person's or one team's, also had its own pieces
here (a team's aliases winning over the writer's, the deletion of a note's
facts): they left with it.
The same triple in two memories is two facts; inside one memory it is one fact
with its sources, reconfirmed instead of duplicated.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import aliased, defer
from sqlalchemy.orm.exc import StaleDataError

from . import contradiction as _contradiction
from . import identity as _id
from . import log_safe as _log
from .families import PREDICATE_TO_FAMILY, family_for
from .predicate_forms import canonicalize_predicate
from . import thresholds as _thresholds
from . import vocabulary as _vocabulary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tables:
    """Which columns play which role in an engine's tables."""
    Fact: Any                      # fact model
    Alias: Any                      # entity alias model
    source: str                      # single source column: source_memory_id
    sources: str                      # JSON list of sources: source_memory_ids
    alias_fact: str                # alias -> identity fact: source_fact_id
    Relation: Any = None           # learned predicate cluster model (PredicateCluster)
    Variant: Any = None            # surface -> cluster model (PredicateAlias)
    # The fact's vector column (korely-agent: "embedding"), left out of the
    # reads that load a whole memory and never use it (`recanonicalize`):
    # 768 floats come back as a Python list of about 30 KiB a row, most of
    # what such a read held (audit of 20 Oct 2026). "" loads it.
    vector: str = ""
    # The alias column holding the canonical as stated, never rewritten by a
    # flattening (stated_canonical): what `remove_alias` puts a chain back
    # from. "" for an engine without it: no chain is put back.
    alias_stated: str = ""
    # Called on a fact whose canonical names `recanonicalize` changed in
    # place: the text the engine embedded is no longer the fact's (Agents:
    # `vector_state.cleared`, the vector computed again from the new text).
    text_changed: Optional[Callable[[Any], None]] = None
    # 6.1.0 (`objects`, off by default): the model of the links "the same
    # as" between two objects of a memory, which no fact rewrite follows
    # (the columns are listed in objects.py). None for an engine without it.
    Link: Any = None


@dataclass(frozen=True)
class Memory:
    """One memory: SQL conditions that select it, and how to create rows in it."""
    facts: Any                                  # condition on Tables.Fact
    alias: Any                                  # condition on Tables.Alias (reads, writes, cycle checks)
    new_alias: Dict[str, Any] = field(default_factory=dict)      # columns of a new alias row
    relations: Any = None                       # condition on Tables.Relation
    variants: Any = None                        # condition on Tables.Variant
    new_relation: Dict[str, Any] = field(default_factory=dict)  # columns of a new cluster / variant row
    links: Any = None                           # condition on Tables.Link (6.1.0, `objects`)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


_MIN = datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# aliases
# ---------------------------------------------------------------------------

def alias_map_of(db, tables: Tables, condition) -> Dict[str, str]:
    """alias_lower -> canonical_label under a condition on the alias table.
    (Until 4.0.0 `vince=`, rows that override the others on the same alias:
    a team's alias over the writer's personal one, in the personal-memory
    engine.)"""
    q = db.query(tables.Alias.alias_lower, tables.Alias.canonical_label).filter(condition)
    return {a: c for a, c in q.all()}


def memory_alias_map(db, tables: Tables, mem: Memory) -> Dict[str, str]:
    """The map a WRITE in this memory canonicalizes with: the memory's own
    aliases. (Until 4.0.0 `Memory.alias_lettura` could widen them, the
    memory's winning: the personal-memory engine's team memory.)"""
    return alias_map_of(db, tables, mem.alias)


def add_alias(
    db,
    tables: Tables,
    mem: Memory,
    alias: str,
    canonical: str,
    *,
    source: str = _id.SOURCE_USER,
    confidence: float = 1.0,
    fact_id: Any = None,
    recanonicalize_now: bool = True,
) -> Tuple[Any, str]:
    """Write "alias -> canonical" in the memory and re-canonicalize its facts.

    Returns (row, outcome); outcome as `identity.new_alias_outcome`.

    `recanonicalize_now=False` writes the alias and leaves the facts to
    the caller, who adding many aliases to one memory runs `recanonicalize`
    once after the last of them (a project's deletion moving its aliases,
    audit of 20 Oct 2026: one full read of the memory per alias before).
    The outcome says whether there is anything to do: only `ALIAS_WRITTEN`
    change the map. The aliases decide nothing from the facts (the map, the
    row, the cycle check read the alias table only), so the alias rows are the
    same; and the facts end with the same names, a function of the final map,
    the same rows kept and merged away (the oldest of each group of open
    duplicates: a batch that only adds aliases, as a move does, only joins
    groups; one that points an alias elsewhere, `"updated"`, can split a
    group a step had merged, and is not for a batch). One difference:
    where the steps would have merged a group two rows at a time (three rows
    or more joined by two aliases of the batch), the one pass merges it in
    age order, so the kept row's sources can be listed in another order, and
    a plan in the group closes where the oldest done fact begins, not the
    first one a step met."""
    m = alias_map_of(db, tables, mem.alias)
    a_low = (alias or "").strip().lower()
    row = (
        db.query(tables.Alias).filter(mem.alias, tables.Alias.alias_lower == a_low).first()
        if a_low else None
    )
    outcome, final_canonical = _id.new_alias_outcome(
        m, alias, canonical,
        source=source,
        existing_source=row.source if row is not None else None,
        existing_canonical=row.canonical_label if row is not None else None,
    )
    if outcome in ("invalid", "conflict"):
        if outcome == "conflict":
            # 3.0.0: the names are entities of the memory's
            # content (a person, a company); the row and the fact say which.
            logger.info("[memory] alias refused (cycle, or decided by hand): existing row %s, fact %s, source %s",
                        getattr(row, "id", None), fact_id, source)
        return None, outcome
    if outcome == "same":
        return row, outcome
    if row is not None:
        row.canonical_label = final_canonical
        row.source = source
        row.confidence = confidence
        setattr(row, tables.alias_fact, fact_id)
        row.since = _now()
    else:
        row = tables.Alias(
            alias_lower=a_low,
            alias_label=alias.strip(),
            canonical_label=final_canonical,
            source=source,
            confidence=confidence,
            **{tables.alias_fact: fact_id},
            **mem.new_alias,
        )
        db.add(row)
    if tables.alias_stated:
        # What was stated, before its own chain resolved it ("GX -> Acme"
        # while "Acme -> Globex" stands is stored pointing at "Globex"): the
        # flattening below never rewrites it, so `remove_alias` knows
        # exactly which rows a rule changed (audit of 20 Oct 2026).
        setattr(row, tables.alias_stated, (canonical or "").strip())
    db.flush()
    # Flattened chain: whoever pointed at the alias now points at its canonical
    # (Gordon Pro -> Gordon, and now Gordon -> Korely: Gordon Pro -> Korely).
    db.query(tables.Alias).filter(
        mem.alias,
        func.lower(tables.Alias.canonical_label) == a_low,
        tables.Alias.id != row.id,
    ).update({tables.Alias.canonical_label: final_canonical}, synchronize_session=False)
    if recanonicalize_now:
        recanonicalize(db, tables, mem)
    # 3.0.0: the row, the fact and the source, never the names.
    logger.info("[memory] alias %s: row %s, fact %s (%s)", outcome, row.id, fact_id, source)
    return row, outcome


# The outcomes of `add_alias` that wrote the map, so that the facts follow.
ALIAS_WRITTEN = ("created", "updated")


def remove_alias(db, tables: Tables, mem: Memory, row) -> dict:
    """"Undo": the row goes, the aliases its flattening rewrote point again
    where they were stated (`restore_the_chain`), the facts' canonicals are
    recomputed. Facts the merge collapsed stay ONE row (with all their
    sources): what was identical cannot be split, and does not need to be."""
    restore_the_chain(db, tables, mem, row)
    db.delete(row)
    db.flush()
    return recanonicalize(db, tables, mem)


def restore_the_chain(db, tables: Tables, mem: Memory, row) -> int:
    """The aliases of the memory whose chain went through `row`, pointed again
    where their chain leads without it. Returns how many changed.

    `add_alias` flattens a chain in place: learning "Acme -> Globex"
    rewrites "GX -> Acme" to "GX -> Globex". The undo used to be inferred (an
    alias pointing at the new name, older than the rule, with no fact of its
    own naming it), and it rewrote a hand-declared "GX -> Globex" made before
    the rename, which the flattening had never touched, to "GX -> Acme"
    (audit of 20 Oct 2026). Now each row keeps what it was stated with
    (`Tables.alias_stated`), and the chain is computed twice from those, with
    `row` and without it (`identity.canonical_of`, the same walk a write
    resolves a name with): a row changes only when the two differ and it
    still points where the chain through `row` leads, which is exactly a row
    some flattening by `row` produced. A row written before the column holds
    its canonical as it is (stated = current), so nothing about it is guessed
    and an undo leaves it. Does not delete `row` nor recompute the facts:
    `remove_alias` does both."""
    if not tables.alias_stated:
        return 0
    rows = db.query(tables.Alias).filter(mem.alias).all()
    stated = {r.alias_lower: (getattr(r, tables.alias_stated) or r.canonical_label) for r in rows}
    stated_without = {k: v for k, v in stated.items() if k != row.alias_lower}
    restored = 0
    for r in rows:
        if r.id == row.id:
            continue
        with_it = _id.canonical_of(stated[r.alias_lower], stated)
        without_it = _id.canonical_of(stated[r.alias_lower], stated_without)
        if with_it.lower() == without_it.lower() or (r.canonical_label or "").strip().lower() != with_it.lower():
            continue
        r.canonical_label = without_it
        restored += 1
    if restored:
        db.flush()
    return restored


# ---------------------------------------------------------------------------
# facts: sources, reconfirmation, merging
# ---------------------------------------------------------------------------

def sources_of(tables: Tables, r) -> List[str]:
    sources = list(getattr(r, tables.sources) or [])
    single_source = getattr(r, tables.source)
    if not sources and single_source is not None:
        sources = [str(single_source)]
    return sources


def reconfirm(tables: Tables, fact, source_id: Any, *, when: Optional[datetime] = None) -> None:
    """The source restates the fact: it joins the sources, the count goes up,
    the last confirmation is `when` (default now) (MemoryBank: the memory
    strengthens). Never backwards (2.8.0): with the confirmation dated when
    the fact was said (`writing.write_fact`), an old memory imported after
    a newer one restates the fact without making it look older."""
    sources = sources_of(tables, fact)
    if source_id is not None:
        sid = str(source_id)
        if sid not in sources:
            sources.append(sid)
        count = len(sources)
    else:
        # A write with no source (a triple posted directly) still restates it.
        count = int(fact.observation_count or 1) + 1
    setattr(fact, tables.sources, sources or None)
    fact.observation_count = max(count, len(sources))
    new = _aware(when) or _now()
    before = _aware(fact.last_confirmed_at)
    fact.last_confirmed_at = new if before is None or new > before else before


def close_the_plan(plan: Any, start: datetime) -> bool:
    """A plan that came true ends when the done fact begins (3.0.0,
    `writing.write_fact`; here since 5.4.0, for `merge_into` too), never before the plan's own start (an empty interval,
    true on no date, when the done fact began first). False, and nothing
    written, when the plan already ends before then (a plan closed on a
    future date, still true today, and done after that date): the two do not
    overlap, and Graphiti invalidates only "temporally overlapping
    contradictions" (section 2.2.3)."""
    born = _aware(plan.valid_from)
    end = max(start, born) if born is not None else start
    already = _aware(plan.invalid_at)
    if already is not None and already <= end:
        return False
    plan.invalid_at = end
    return True


def merge_into(db, tables: Tables, keep, loser) -> Any:
    """The loser flows into the keeper: sources united, count, the latest
    confirmation; whoever pointed at the loser now points at the keeper; the
    loser is removed (NOT invalidated: nothing stopped being true). Returns
    the row that stays open.

    5.4.0: a plan and the same thing done are not one row. Until then a
    planned keeper took the loser's "current" in place, against the rule of
    3.0.0 (CONTRIBUTING.md: a plan that comes true stays in history, never a
    promotion in place; Graphiti, Rasmussen et al. 2025, §2.2.3), and the plan
    was gone from `as_of` (a code review found it). Now the plan is
    closed where the done fact begins (`close_the_plan`), pointing at it,
    and the done fact is the row that stays open, as on a write."""
    if keep.tense == "planned" and loser.tense == "current":
        start = _aware(loser.valid_from) or _aware(loser.created_at) or _now()
        if close_the_plan(keep, start):
            keep.invalidated_by_id = loser.id
            mark_closed(db, keep.id)
        return loser
    sources = sources_of(tables, keep)
    for s in sources_of(tables, loser):
        if s not in sources:
            sources.append(s)
    last = _aware(keep.last_confirmed_at) or _aware(keep.created_at)
    lc = _aware(loser.last_confirmed_at) or _aware(loser.created_at)
    if lc is not None and (last is None or lc > last):
        last = lc
    if (loser.confidence or 0) > (keep.confidence or 0):
        keep.confidence = loser.confidence
    if getattr(keep, "predicate_family", None) is None and getattr(loser, "predicate_family", None):
        keep.predicate_family = loser.predicate_family
    if hasattr(keep, "session_only") and hasattr(loser, "session_only"):
        # 5.7.0: a fact is only a work session's while no note states it
        # (korely-agent's AgentFact.session_only, 18 Oct 2026): the merged
        # row is a note's as soon as either side was (the code audit of 18
        # Oct 2026).
        keep.session_only = bool(keep.session_only) and bool(loser.session_only)
    setattr(keep, tables.sources, sources or None)
    if sources:
        keep.observation_count = len(sources)
    else:
        keep.observation_count = int(keep.observation_count or 1) + int(loser.observation_count or 1)
    keep.last_confirmed_at = last
    F = tables.Fact
    db.query(F).filter(F.invalidated_by_id == loser.id).update(
        {F.invalidated_by_id: keep.id}, synchronize_session=False,
    )
    col = getattr(tables.Alias, tables.alias_fact)
    db.query(tables.Alias).filter(col == loser.id).update({col: keep.id}, synchronize_session=False)
    db.delete(loser)
    return keep


def _key(sc: str, pred: str, oc: str) -> Tuple[str, str, str]:
    return ((sc or "").strip().lower(), (pred or "").strip(), (oc or "").strip().lower())


def recanonicalize(
    db,
    tables: Tables,
    mem: Memory,
    *,
    new_predicates: Optional[Dict[str, str]] = None,
    new_family: Optional[Dict[str, str]] = None,
    extra_key: Optional[Callable[[Any], tuple]] = None,
) -> dict:
    """Recompute every fact of the memory from the current alias map (and,
    when given, a predicate rename from the learned vocabulary), and collapse
    the open duplicates the merge produced ("Gordon uses FastAPI" + "Korely
    uses FastAPI" -> one row).

    Two passes with a flush in between, not one: a unique index on the open
    canonical triple is checked on every UPDATE, and if the keeper took the new
    key while the loser was still there the write would fail. First the losers
    go, then the survivors get their new names.

    `extra_key(row)` adds columns that separate memories inside the
    condition (Agents: the agent namespace, which is part of the dedup key).

    The vector (`Tables.vector`) is not read: names and duplicates are
    decided on the labels, and a row that changes keeps its vector (audit
    of 20 Oct 2026: the read held every vector of the memory).
    """
    alias_map = memory_alias_map(db, tables, mem)
    q = db.query(tables.Fact).filter(mem.facts)
    if tables.vector:
        q = q.options(defer(getattr(tables.Fact, tables.vector)))
    rows = q.all()
    pred_map = new_predicates or {}
    fam_map = new_family or {}

    new_facts: Dict[Any, Tuple[str, str, str]] = {}
    for r in rows:
        pc = pred_map.get(r.predicate_canonical, r.predicate_canonical)
        sc, oc = _id.canonicals_of_the_triple(r.subject_label, pc, r.object_label, alias_map)
        new_facts[r.id] = (sc, pc, oc)

    groups: Dict[tuple, list] = {}
    for r in rows:
        if r.invalid_at is not None:
            continue
        sc, pc, oc = new_facts[r.id]
        k = _key(sc, pc, oc) + (extra_key(r) if extra_key else ())
        groups.setdefault(k, []).append(r)
    collapsed = 0
    plans_closed = 0
    survivors = [r for r in rows if r.invalid_at is not None]
    for grp in groups.values():
        if len(grp) == 1:
            survivors.append(grp[0])
            continue
        grp.sort(key=lambda x: (_aware(x.created_at) or _MIN, str(x.id)))
        keep, losers = grp[0], grp[1:]
        for p in losers:
            stays = merge_into(db, tables, keep, p)
            if stays is keep:
                collapsed += 1
            else:
                # 5.4.0: a plan closed by the done fact keeps its row, and its
                # names are recomputed with the others.
                survivors.append(keep)
                plans_closed += 1
                keep = stays
        survivors.append(keep)
    if collapsed or plans_closed:
        db.flush()

    changed = 0
    for r in survivors:
        sc, pc, oc = new_facts[r.id]
        names = changed
        if (r.subject_canonical or "") != sc:
            r.subject_canonical = sc
            changed += 1
        if (r.object_canonical or "") != oc:
            r.object_canonical = oc
            changed += 1
        if changed != names and tables.text_changed is not None:
            # The names changed in place, so a vector computed on the old ones
            # ("Globex uses Slack" after the rename is undone) no longer
            # describes the fact (audit of 20 Oct 2026): the engine says
            # so, and Agents computes it again from the current text.
            tables.text_changed(r)
        if r.predicate_canonical != pc:
            r.predicate_canonical = pc
            changed += 1
        fam = fam_map.get(pc)
        if fam and getattr(r, "predicate_family", None) != fam:
            r.predicate_family = fam
            changed += 1
    db.flush()
    outcome = {"facts": len(rows), "changed": changed, "collapsed": collapsed,
             "plans_closed": plans_closed}
    if changed or collapsed or plans_closed:
        logger.info("[memory] recanonicalized: %s", outcome)
    return outcome


def find_open(
    db,
    tables: Tables,
    mem: Memory,
    subject_canonical: str,
    predicate_canonical: str,
    object_canonical: str,
    *,
    exclude_id: Any = None,
    is_open: Any = None,
):
    """The open fact with the same canonical triple in the same memory: the
    one to RECONFIRM instead of inserting a new row. `is_open` overrides what
    "open" means (Agents: `true_at()`, which also counts a fact closed on a
    future date as still true today); without it, the predicate of the
    engines' unique index on the open triple, `invalid_at IS NULL`.

    The names are lowercased by the database on BOTH sides (2.11.0), with the
    `lower()` the unique index is built on. The side of the parameter used to
    be Python's `str.lower()`, which is not the same function: Python applies
    Unicode's full case mapping, so "İstanbul" becomes nine characters ("i"
    and a combining dot), while PostgreSQL's `lower()` gives "istanbul", eight
    (measured on the dev database, PostgreSQL 15, libc collation en_US.utf8,
    6 Oct), and SQLite's lowercases ASCII only. On such a name this read found
    nothing, the insert then hit the index, the read after the race found
    nothing again, and the write failed every time the fact was restated
    (code audit of 6 Oct)."""
    F = tables.Fact
    q = db.query(F).filter(
        mem.facts,
        is_open if is_open is not None else F.invalid_at.is_(None),
        func.lower(F.subject_canonical) == func.lower((subject_canonical or "").strip()),
        F.predicate_canonical == predicate_canonical,
        func.lower(F.object_canonical) == func.lower((object_canonical or "").strip()),
    )
    if exclude_id is not None:
        q = q.filter(F.id != exclude_id)
    return q.order_by(F.created_at.asc()).first()


def open_twins(db, tables: Tables, source_filter: Any, beside: Callable[[Any, Any], Any]) -> List[Tuple[Any, Any]]:
    """Every open fact under the condition `source_filter` with its open twin, as
    (fact id, twin id): `find_open` for many facts in one read, for a move
    of facts between memories (a project's deletion, audit of 20 Oct 2026:
    one `find_open` per fact, every fact and every twin read whole,
    vector included). `beside(F, T)` is the condition on `T`, an alias of
    the fact table, that puts it in the memory `F` moves to (tenant, project,
    end user, agent namespace). Only the ids come back, one twin per fact,
    the oldest, as `find_open` takes it; nothing else is loaded.

    The triple is the one of `find_open`: the canonical names lowercased by
    the database on both sides (2.11.0), the relation equal. `find_open`
    strips the fact's names in Python before; here the database's `trim`
    does, the same on every name the engine writes, which are stripped when
    written (`identity.canonical_of`)."""
    F = tables.Fact
    T = aliased(F)

    def name(col):
        return func.lower(func.trim(func.coalesce(col, "")))

    q = (db.query(F.id, T.id)
           .join(T, and_(beside(F, T),
                         T.invalid_at.is_(None),
                         func.lower(T.subject_canonical) == name(F.subject_canonical),
                         T.predicate_canonical == F.predicate_canonical,
                         func.lower(T.object_canonical) == name(F.object_canonical)))
           .filter(source_filter, F.invalid_at.is_(None))
           .order_by(F.id, T.created_at.asc()))
    pairs: List[Tuple[Any, Any]] = []
    seen: set = set()
    for fid, tid in q:
        if fid not in seen:
            seen.add(fid)
            pairs.append((fid, tid))
    return pairs


# What a write has closed (3.1.3, piece 6 of the 3.1 plan).
_CLOSED_BY_THE_WRITE = "memory_core.closed_by_the_write"


@contextmanager
def one_write(db):
    """The facts one write of a memory closes, kept on the session while the
    write runs (`worker.write_memory`), so that every judgement of the
    write weighs the facts as they were before it (`as_before_the_write`).

    Zep/Graphiti resolve all the edges of one episode against the graph as it
    was before the episode: `resolve_extracted_edges` gathers, for every edge
    extracted from it, the related edges and the invalidation candidates with
    searches run side by side (`semaphore_gather`), decides each edge on those,
    and the invalidations are saved with the episode afterwards
    (getzep/graphiti, graphiti_core/utils/maintenance/edge_operations.py and
    graphiti_core/graphiti.py `add_episode`, read at 689de29 of 2026-10-06;
    Rasmussen et al. 2025, arXiv:2501.13956, section 2.2.3). Here the facts of
    one write are written one after the other, and until 3.1.3 a fact closed by
    an earlier statement of the same write had left the candidates of the later
    ones. The Cloud acceptance run of 3.0.0 (11/12 twice): from "Globex moved to the
    Starter plan." the extractor also wrote "Globex replaces Business plan" in
    the past tense; judged first, it closed the plan, and the batch of
    "subscribes_to Starter plan" saw Slack as the only subscription and closed
    it (rebuilt with the candidates: 7 of 8 with the judge of 2.11.0, 5 of 8
    with that of 2.12.0; with the plan among the candidates 0 of 10 for both).

    Only the candidates widen. What is true now (`find_open`, `close`) does
    not: a fact the write closed is closed, and naming it again closes nothing."""
    before = db.info.get(_CLOSED_BY_THE_WRITE)
    db.info[_CLOSED_BY_THE_WRITE] = set()
    try:
        yield
    finally:
        if before is None:
            db.info.pop(_CLOSED_BY_THE_WRITE, None)
        else:
            db.info[_CLOSED_BY_THE_WRITE] = before


def mark_closed(db, fact_id: Any) -> None:
    """A fact the running write closed (`one_write`); nothing outside a write."""
    closed = db.info.get(_CLOSED_BY_THE_WRITE)
    if closed is not None and fact_id is not None:
        closed.add(fact_id)


def as_before_the_write(db, F: Any, is_open: Any):
    """What "still true" means for the candidates of a judgement: `is_open`, and
    the facts the running write has closed, which were true when it began
    (`one_write`). Outside a write, `is_open` itself."""
    closed = db.info.get(_CLOSED_BY_THE_WRITE)
    if not closed:
        return is_open
    return or_(is_open, F.id.in_(sorted(closed, key=str)))


def lose_the_source(tables: Tables, r: Any, source_id: Any) -> bool:
    """`r` no longer has `source_id` among its sources, when another source
    still states it: the sources without it, the count, and the first source
    re-pointed to one of the others. True when that is what happened; False,
    and `r` untouched, when no other source states it (the caller decides:
    closed by an edit, `retire_the_previous_version`, or by a forget). The rule
    of an edit and of a forget alike: Graphiti's `remove_episode` keeps an
    edge "that other episodes also support" and only takes the episode out of
    its `episodes` (getzep/graphiti, graphiti_core/graphiti.py, read at
    72b5c59 of 2026-10-31)."""
    sid = str(source_id)
    sources = [x for x in sources_of(tables, r) if x != sid]
    if not sources:
        return False
    setattr(r, tables.sources, sources)
    r.observation_count = len(sources)
    if str(getattr(r, tables.source)) == sid:
        setattr(r, tables.source, _uuid_like(getattr(r, tables.source), sources[0]))
    return True


# The facts a running write's batched judgement may weigh although the
# memory being written is among their sources (6.2.0, `judged_apart`).
_JUDGED_APART = "memory_core.judged_apart"


def shared_by_the_previous_version(
    db, tables: Tables, source_id: Any, kept: set, *, is_open: Any, scope: Any = None,
) -> set:
    """6.2.0: the ids of the open facts the memory `source_id` stated with
    other sources and that this write did not write or restate (`kept`): the
    copies its previous text shared, which the new text no longer states.
    Read only (ids, no row loaded, nothing locked), after the new text's
    facts are written and before the batched judgement, which weighs them
    as candidates (`judged_apart`); `retire_the_previous_version(apart=)`
    then takes the memory out of their sources. `is_open` and `scope` as
    there.

    Why (the copies, mem0ai/mem0 issue #5867): "I work at Acme" stated by
    three memories is one row with three sources (`reconfirm`). One of them
    edited to "now I work at Beta" left Acme open beside Beta, whatever the
    judge would have said, because the judge was never asked: a memory's
    own facts are never its candidates (BUG-123, `outside_the_source`), and
    Acme still listed the edited memory, whose PREVIOUS text had said it.
    The retirement then only took the memory out of Acme's sources, after
    the judgement. A new memory saying Beta was judged against Acme and
    closed it; an edit, the same words from a memory that used to say Acme,
    did not. BUG-123 keeps the statements of ONE text from closing each
    other, and the old text is no longer the memory's.

    Graphiti has no edit of an episode: a changed episode is removed
    (`remove_episode` keeps an edge other episodes support, without it) and
    added again, and `add_episode` resolves the new episode's edges against
    the graph as it is then, every edge of the group a candidate for
    invalidation (`resolve_extracted_edges`, graphiti_core/utils/maintenance/
    edge_operations.py; both read at 72b5c59 of 2026-10-31), with the
    temporal invalidation of Rasmussen et al. 2025 (arXiv:2501.13956,
    section 2.2.3): an existing edge a new one contradicts ends where the new
    one begins. Here the copy is weighed as if the memory were not among its
    sources by the judgement of the whole write, which comes after every
    statement of the new text is written (`worker.write_memory(batch=)`): what
    the new text states again is known by then and is never a candidate of
    its own statements, whatever their order. A judgement made while the text
    is still being written (a past statement's, or every one without `batch`)
    does not see the copies, as before 6.2.0: it cannot know yet which of
    them the text states again. Nothing is written before the judge answers,
    so the copies are not locked while the model is asked."""
    F = tables.Fact
    sid = str(source_id)
    q = db.query(F.id, getattr(F, tables.source), getattr(F, tables.sources)).filter(
        source_condition(tables, source_id, scope=scope), is_open)
    if kept:
        q = q.filter(~F.id.in_(list(kept)))
    out = set()
    for fid, first, listed in q.all():
        sources = list(listed or []) or ([str(first)] if first is not None else [])
        if any(str(x) != sid for x in sources):
            out.add(fid)
    return out


@contextmanager
def judged_apart(db, source_id: Any, fact_ids: set):
    """While it runs, the judgement of the memory `source_id` weighs the
    facts `fact_ids` as if the memory were not among their sources
    (`apart_from`, read by `candidates.for_the_judge`): the copies its
    previous text shared (`shared_by_the_previous_version`). Kept on the
    session as `one_write` keeps its closings; outside it, nothing."""
    before = db.info.get(_JUDGED_APART)
    db.info[_JUDGED_APART] = (str(source_id), frozenset(fact_ids or ()))
    try:
        yield
    finally:
        if before is None:
            db.info.pop(_JUDGED_APART, None)
        else:
            db.info[_JUDGED_APART] = before


def apart_from(db, source_id: Any) -> frozenset:
    """The fact ids `judged_apart` admits for `source_id`; empty otherwise."""
    held = db.info.get(_JUDGED_APART)
    if not held or source_id is None or held[0] != str(source_id):
        return frozenset()
    return held[1]


def _ended_by_the_edit(r: Any, when: Optional[datetime]) -> None:
    """A fact the previous text alone stated ends when the new text arrived,
    never before it began (2.11.0)."""
    end = _aware(when) or _now()
    born = _aware(getattr(r, "valid_from", None))
    r.invalid_at = born if born is not None and born > end else end


def retire_the_previous_version(
    db, tables: Tables, source_id: Any, kept: set, *, when: Optional[datetime], is_open: Any,
    scope: Any = None, apart: Sequence[Any] = (),
) -> List[Any]:
    """A memory was extracted again after its text changed: the facts it
    stated before and does not state now stop being true when the new text
    arrived (2.3.0). `kept`: the ids this round wrote or restated. A fact
    only this memory stated is CLOSED at `when` (not deleted: the docs
    promise that a changed value supersedes the prior fact, with its history
    and `as_of`; the personal-memory engine deleted instead, because a note is
    a living document). A fact other sources still state loses this one and
    stays open. `is_open`: the engine's "true now". Returns the closed facts.

    Found by the public conformance test of 29 Sep: "prefers email" edited to
    "now prefers phone" left likes email, dislikes phone, likes phone and
    dislikes email all open, because a memory's facts never close each other
    (BUG-123), and that rule held across two versions of the same text.

    2.11.0: a fact is never closed before it began. One whose valid_from is
    after `when` (a write dated ahead) closes at its valid_from: true on no
    date, rather than an end earlier than its start. An engine that wants
    such facts retired too passes as `is_open` "not ended yet" (`invalid_at`
    NULL or in the future) instead of "true now", which leaves them out
    (the cloud does since 2.11.0: left out, the edited memory's old fact
    became true at its date).

    `scope` (2.12.0, code audit of 6 Oct): the memory's scope, a
    condition on `Tables.Fact` (Agents: account, project, end user). This
    runs at EVERY write of a memory, and `source_condition` without it is an
    OR with a LIKE on the text of the JSON sources: no index can serve the
    second branch, so PostgreSQL read the whole facts table, every
    customer's. Measured on the dev database (PostgreSQL 15, 121,379 facts
    of 200 accounts generated inside a transaction rolled back): a Parallel
    Seq Scan, 3,402 buffers, 41 ms; with `scope` a BitmapOr of the source
    index and the account+project index, 600 rows, 1.0 ms. A fact is born
    from a memory of its own scope and reconfirmed only within it (the
    open-triple index is per memory), as `states_of_the_sources` already relies
    on. Without it, the whole table, as before.

    `apart` (6.2.0): the copies the previous text shared and the new one
    does not state (`shared_by_the_previous_version`), which the write's
    judgement weighed. The memory leaves their sources whatever the judge
    said, closed by it or still open (`lose_the_source`). The copy the judge
    closed in this very write is the one place a CLOSED row loses a source:
    a forget, and this function's other rows, touch only facts not ended.
    Kept on it, the edited memory would be a live source whose current text
    arrived after the closing, which the product's give-back reads as later
    word on the fact (`given_back._vouching` in korely-agent), so retracting
    the new value later would leave the copy closed although the memories
    still saying it are live. One no other source states any more
    (forgotten meanwhile) is closed with the rest, if still open. They are
    locked and read again first, in id order: another write may have changed
    their sources since this one began (two copies edited at once), and the
    sources are rewritten from what is committed, not from a read made
    before the model was asked."""
    F = tables.Fact
    sid = str(source_id)
    closed: List[Any] = []
    others = [i for i in apart if i not in kept]
    if others:
        db.flush()
        rows = (db.query(F).filter(F.id.in_(others)).order_by(F.id)
                .with_for_update().populate_existing().all())
        still = {i for (i,) in db.query(F.id).filter(F.id.in_(others), is_open).all()}
        for r in rows:
            if sid not in sources_of(tables, r) or lose_the_source(tables, r, source_id):
                continue
            if r.id in still:
                _ended_by_the_edit(r, when)
                closed.append(r)
    q = db.query(F).filter(source_condition(tables, source_id, scope=scope), is_open)
    done = set(kept) | set(others)
    if done:
        q = q.filter(~F.id.in_(list(done)))
    for r in q.all():
        if lose_the_source(tables, r, source_id):
            continue
        _ended_by_the_edit(r, when)
        closed.append(r)
    if closed:
        logger.info("[memory] %d fact(s) of the previous version of %s closed", len(closed), sid[:8])
    db.flush()
    return closed


def state_of_the_source(db, tables: Tables, source_id: Any, *, is_open: Any) -> Tuple[int, int]:
    """(facts the source stated, those still standing): what
    `presentation.memories_for_the_context` needs to know about a memory.

    Standing = true now, or closed only by its tense. A fact stated in the
    past tense ("I redeemed a $5 coupon last Sunday") is history: `writing`
    stores it born closed (Graphiti section 2.2.3, the tense is the model's),
    and nothing newer replaced it. Until 2.5.1 it counted as no longer true,
    so the memory of what happened left the context even when the search put
    it first (LongMemEval run A, 2026-10-01, measured on the
    self-hosted engine: evidence in the context 74% -> 80% at 2,000 tokens,
    85% -> 94% at 4,000). A fact closed by a newer fact (`invalidated_by_id`
    set) stays superseded. A present-tense statement that a later memory
    turned into history counts as standing too: it was true when it was said,
    and the closing memory is in the context beside it. So does a plan that
    came true (3.0.0, `still_standing`)."""
    from sqlalchemy import func

    F = tables.Fact
    base = db.query(func.count(F.id)).filter(source_condition(tables, source_id))
    return int(base.scalar() or 0), int(base.filter(still_standing(tables, is_open)).scalar() or 0)


def still_standing(tables: Tables, is_open: Any):
    """SQL condition "this fact still stands" for the context's memories
    (`state_of_the_source`, `states_of_the_sources`): true now (`is_open`); or
    closed only by its tense, a past statement no newer fact replaced
    (2.5.1); or a plan that came true (3.0.0).

    Until 3.0.0 a plan said again as done stayed one open row, so the memory
    of the plan stood. Now the plan is closed by the done fact
    (`writing.write_fact`, Graphiti's history of a relationship): it was
    not made false, it happened, and the memory that told the plan is history
    as a past statement's is (LongMemEval, Wu et al., ICLR 2025, asks how long
    after a plan a thing was done). A plan that came true is one closed by a
    fact of the same canonical triple that is not a plan. A plan in other
    words closed by `twins` (off) has another object and stays superseded;
    a plan closed by the judge (a contradiction) too."""
    from sqlalchemy import and_, exists, func, or_
    from sqlalchemy.orm import aliased

    F = tables.Fact
    G = aliased(F)
    came_true = and_(
        F.tense == "planned",
        F.invalidated_by_id.isnot(None),
        exists().where(and_(
            G.id == F.invalidated_by_id,
            or_(G.tense.is_(None), G.tense != "planned"),
            func.lower(G.subject_canonical) == func.lower(F.subject_canonical),
            G.predicate_canonical == F.predicate_canonical,
            func.lower(G.object_canonical) == func.lower(F.object_canonical),
        )),
    )
    return or_(is_open, and_(F.tense == "past", F.invalidated_by_id.is_(None)), came_true)


def states_of_the_sources(
    db, tables: Tables, sources: Sequence[Any], *, is_open: Any, scope: Any = None,
) -> Dict[str, Tuple[int, int]]:
    """{source id as text: (facts it stated, those still standing)} for many
    sources in ONE query: the rule of `state_of_the_source`, asked once per
    context block instead of twice per memory (BUG-367, 2.8.0).

    `state_of_the_source` counts over the whole facts table, every tenant's: its
    OR with a LIKE on the text of the JSON sources keeps Postgres off every
    index, so each count is a sequential scan and the context grows with the
    table, not with the user. Measured on 2026-10-05 on the bench
    copy (one end user with 3,571 memories and 5,348 facts, a table of 20,784):
    80 such queries per context, 75% of the call, median 0.30 s.

    Same rule, in Python over the rows of one query. `scope` (a condition on
    `Tables.Fact`, the caller's memory: account, end user) bounds the scan:
    a fact is born from a memory of its own memory and reconfirmed within its
    own triples (the open-triple index is per memory), so no fact outside it
    can count. Without `scope` the whole table, as `state_of_the_source`."""
    from sqlalchemy import String, and_, cast, or_

    keys = [str(f) for f in sources if f is not None]
    if not keys:
        return {}
    F = tables.Fact
    born_from_col = getattr(F, tables.source)
    sources_col = getattr(F, tables.sources)
    q = db.query(born_from_col, sources_col, F.observation_count, still_standing(tables, is_open).label("standing")).filter(
        or_(
            born_from_col.in_([f for f in sources if f is not None]),
            and_(F.observation_count > 1, or_(*[cast(sources_col, String).like(f"%{k}%") for k in keys])),
        )
    )
    if scope is not None:
        q = q.filter(scope)
    counts = {k: [0, 0] for k in keys}
    for born_from, listed, seen, stands in q.all():
        text = str(listed) if listed is not None else ""
        for k, count in counts.items():
            if (born_from is not None and str(born_from) == k) or ((seen or 0) > 1 and k in text):
                count[0] += 1
                count[1] += 1 if stands else 0
    return {k: (states, true_ones) for k, (states, true_ones) in counts.items()}


def _uuid_like(example: Any, value: str):
    """The source id in the same type as the column holds (UUID or str)."""
    if example is None or isinstance(example, str):
        return value
    return type(example)(value)


def source_condition(tables: Tables, source_id: Any, *, scope: Any = None):
    """Rows the source asserted: born from it, or reconfirmed by it. The LIKE
    on the JSON text works the same on Postgres and on the SQLite of the test
    suites; shared rows are few per memory.

    `scope` (2.12.0): a condition on `Tables.Fact` that bounds the second
    branch, the source's scope. No index serves a LIKE on the JSON text, and
    without a bound that branch is a scan of the whole table, every
    customer's (`retire_the_previous_version`). In the second branch only:
    the first stays on the source's own index, whatever the scope."""
    from sqlalchemy import String, and_, cast, or_

    F = tables.Fact
    reconfirmed = and_(
        F.observation_count > 1,
        cast(getattr(F, tables.sources), String).like(f"%{source_id}%"),
    )
    if scope is not None:
        reconfirmed = and_(scope, reconfirmed)
    return or_(getattr(F, tables.source) == source_id, reconfirmed)


def outside_the_source(tables: Tables, source_id: Any, *, apart: Sequence[Any] = ()):
    """SQL form of `contradiction.same_source` (BUG-123): the rows this source
    did NOT state, neither as their first source nor as a reconfirmation.

    In the query and not after it, so a LIMIT counts only rows the judge may
    see: filtered afterwards, a note listing more alternatives than the LIMIT
    would fill it with its own facts and leave the real candidates out. NULL
    safe on both columns (a fact written with no source is always a candidate;
    a JSON `null` reads as the text 'null'). The LIKE on the JSON text is the
    one `source_condition` uses, the same on Postgres and on SQLite. No source
    -> no condition.

    `apart` (6.2.0): fact ids weighed as if the source were not among their
    sources (`judged_apart`, the copies an edited memory's previous text
    shared)."""
    from sqlalchemy import String, and_, cast, or_, true

    if source_id is None:
        return true()
    F = tables.Fact
    single_source = getattr(F, tables.source)
    listed = getattr(F, tables.sources)
    outside = and_(
        or_(single_source.is_(None), single_source != source_id),
        or_(listed.is_(None), ~cast(listed, String).like(f"%{source_id}%")),
    )
    if apart:
        return or_(outside, F.id.in_(sorted(apart, key=str)))
    return outside


def without_the_same_source(tables: Tables, candidates: Sequence, source_id: Any, *,
                            apart: Sequence[Any] = ()) -> list:
    """The candidates the source did not state (`contradiction.same_source`),
    read from the columns of each row: the arbiter on the final candidate list,
    after `outside_the_source` has kept the LIMITs honest. `apart` as there."""
    return [c for c in candidates
            if c.id in apart or not _contradiction.same_source(source_id, sources_of(tables, c))]


# ---------------------------------------------------------------------------
# the learned predicate vocabulary (BUG-109)
# ---------------------------------------------------------------------------

class ResolutionUnavailable(RuntimeError):
    """The model was asked and never answered.

    Distinct from the model answering "this phrase names a new relation",
    which is a decision and is kept. A failed call is not a decision: treating
    it as "new" would write a variant row consulted before the model on every
    later write, and one 429 during a retry storm would bind `moved_to` to its
    own relation forever, so `moved_to` and `subscribes_to` never meet and the
    functional supersede never fires. Found on a real self-hosted install:
    the drain reported success over a store holding two contradictory plans.
    """


class ResolutionUnusable(RuntimeError):
    """The model answered, and the answer is none of those the question allows
    (2026-09-28, `vocabulary.UnreadableAnswer`). Raised by
    `resolve_predicate(..., severe=True, strict=True)`; nothing was cached.

    Not a `ResolutionUnavailable`, on purpose: the two need different
    handling. A call that never
    answered is worth retrying; an answer that cannot be used was paid for,
    and at temperature 0 the same prompt gives it again, so a retry loop would
    pay for it again and again. Read as a decision instead, it was cached for
    good: "a new relation" that no later phrase joins, or an "undecided"
    cardinality that disables the supersede of that relation for ever.
    """


@dataclass
class Relation:
    canonical: str
    is_functional: Optional[bool]
    family: str
    new: bool = False
    resolved: bool = True   # False = no usable answer from the model, nothing cached
    # The object the cardinality was measured on ("Business plan"): the type
    # witness of typed functionality (Lin, Mausam, Etzioni, EMNLP 2010 §3).
    example: Optional[str] = None


def _pg(db) -> bool:
    return getattr(getattr(getattr(db, "bind", None), "dialect", None), "name", "") == "postgresql"


def _candidates(db, R, mem: Memory, vec) -> list:
    """The known relations nearest to the phrase's vector, within
    the shortlist cutoff of the embedding model in use (`thresholds.current().shortlist`;
    Postgres + pgvector only)."""
    dist = R.centroid.cosine_distance(vec).label("d")
    rows = (
        db.query(R, dist)
        .filter(mem.relations, R.centroid.isnot(None))
        .order_by(dist)
        .limit(_vocabulary.SHORTLIST)
        .all()
    )
    threshold = _thresholds.current().shortlist
    return [c for c, d in rows if d is None or float(d) <= threshold]


# The families of our own schema (3.3.0): the names the
# extraction prompt asks for, not English words read in a sentence. A
# `renamed_to` is structure the alias rule reads (`identity.py`); every other
# family is the model's answer (`vocabulary.family_question`), since 3.3.0
# the only path: `families.PREDICATE_TO_FAMILY` holds these predicates and no
# other. `instructions` (2.8.0, `instructs`) left with 5.0.0.
_SCHEMA_FAMILIES = ("identity",)


def _schema_family(surface: str) -> Optional[str]:
    fam = PREDICATE_TO_FAMILY.get(surface)
    return fam if fam in _SCHEMA_FAMILIES else None


def schema_family(surface: str) -> str:
    """The family of a relation of our own schema, else "other" (the model is
    asked when the relation is born)."""
    return _schema_family(surface) or "other"


def without_the_object(form: str, obj: str) -> str:
    """The present form of a past phrase without the object at its end (3.3.0,
    the judge's cases of 8 Oct). The question shows the object as context
    ("... left Apple"), and gpt-oss-120b answered "leaves Apple": the relation
    was born as `leaves_apple`, and then took other objects ("Luca
    leaves_ferrero Apple", "Kenji lives_in_kyoto Kyoto"). In Open IE the
    relation phrase and its two arguments are separate spans of the sentence
    (ReVerb, Fader, Soderland and Etzioni, EMNLP 2011, §3): the words of the
    fact's own object, at the end of the answer, are the argument and go.
    Nothing is matched against a list: only against this fact's object. The
    answer stays as it is when nothing of the object ends it, or when what is
    left would be shorter than three letters."""
    if not obj or not form or form == "unknown":
        return form
    words = set(canonicalize_predicate(obj).split("_")) - {"unknown", ""}
    pieces = form.split("_")
    while len(pieces) > 1 and pieces[-1] in words:
        pieces.pop()
    rest = "_".join(pieces)
    return rest if rest != form and len(rest.replace("_", "")) >= 3 else form


def resolve_predicate(
    db,
    tables: Tables,
    mem: Memory,
    predicate_raw: str,
    object_label: str = "",
    *,
    ask: Optional[Callable[[str, bool], Optional[str]]],
    embed: Optional[Callable[[str], Optional[list]]],
    strict: bool,
    tense: Optional[str] = None,
    severe: bool = False,
    single_value_votes: int = 1,
    lazy_votes: bool = False,
    merge: bool = True,
) -> Relation:
    """Map a raw relation phrase to this memory's canonical relation.

    `tense` is the tense the extractor gave the fact. An UNSEEN phrase with
    tense "past" is first brought to its present form by the model
    (`vocabulary.phrase_tense_question`, CESI §4.1 side information) and
    resolved as that, then cached under the phrase as written: "formerly_used"
    becomes a variant of "uses", asked once.

    `ask(prompt, think)` calls the engine's model and returns the raw text
    (or raises); `think=False` asks without thinking. `embed(text)` returns
    a 768D vector or None, and raises when the call fails (on Postgres that
    is a failed resolution, like a failed `ask`). Either may be None: no
    model means every phrase is its own relation (the store works, it just
    does not merge).

    `strict`: when the model fails, raise `ResolutionUnavailable` (Agents:
    the write is not persisted and is retried) or return the surface form
    WITHOUT caching anything (the personal-memory engine: background,
    best-effort; the next write of the same phrase asks again).

    `severe` (1.3.0): an answer that is none of those its question allows
    (`vocabulary.UnreadableAnswer`: empty, not JSON, the key missing, a
    value out of range) is not read as "a new relation", "undecided", "other"
    or "not past", which would be cached for good. With `strict` it raises
    `ResolutionUnusable`, otherwise the surface form comes back uncached
    (`resolved=False`), as for a failed call. {"same_as": null} is still a
    new relation. Off by default: the lenient reading is what the callers
    written before 1.3.0 expect.

    `single_value_votes` (2.1.0): how many answers decide whether a NEW
    relation holds one value at a time, by majority
    (`vocabulary.decide_single_value`; the answer is kept for good). 1 is
    the behaviour before, for callers whose tests script one answer per
    question; the engine's earlier callers passed `vocabulary.SINGLE_VALUE_VOTES`.

    `lazy_votes` (3.3.0, work item 8, off): a NEW relation is born with its
    cardinality undecided, and the votes are asked when a judge first needs
    them (`decide_if_missing`): when a new fact meets an earlier fact of the
    same relation, the only place the rule of the tag speaks of
    (`contradiction.RULE_SINGLE_VALUE`: "a new value OF THAT KIND for the
    same relation"). Undecided reads as today: no tag.

    `merge` (3.3.0, BUG-202): False for a relation the caller named itself
    (a fact written directly through the API, or a correction): the phrase is
    never merged into another relation by the model's "same relation?"
    answer; it is reused when a relation of that very name, or a variant
    already learned for it, exists, else it is born under its own name. A
    developer who wrote `plan` read it back as `role`, and the merge stayed
    for every later write ("developer" is also a job). The facts the
    extractor writes can still merge into it.

    3.3.0 (the rule of 8 Oct, no word lists in the code): the phrase keeps
    the form the model wrote (`predicate_forms.canonicalize_predicate`), and
    only the relations of our own schema, `identity`, have a family without
    asking
    (`schema_family`): the family of every other new relation is
    asked to the model (BUG-109).
    """
    surface = canonicalize_predicate(predicate_raw)
    static_family = schema_family(surface)
    if surface == "unknown":
        return Relation(surface, None, "other")
    # Identity predicates are structure, not vocabulary: `renamed_to` must stay
    # `renamed_to` for the alias rule to read it.
    if static_family == "identity":
        return Relation(surface, None, static_family)

    V, R = tables.Variant, tables.Relation
    already = _from_the_variant(db, tables, mem, surface)
    if already is not None:
        return already

    if tense == "past" and ask is not None:
        past_form = _to_the_present(
            db, tables, mem, surface, static_family, object_label,
            ask=ask, embed=embed, strict=strict, severe=severe,
            single_value_votes=single_value_votes, lazy_votes=lazy_votes, merge=merge,
        )
        if past_form is not None:
            return past_form

    vec, ended_with = _the_vector(db, surface, static_family, object_label, embed, strict=strict)
    if ended_with is not None:
        return ended_with
    shortlist, ended_with = _the_shortlist(db, R, mem, vec, surface, static_family, strict=strict)
    if ended_with is not None:
        return ended_with
    choice, functional, family, ended_with = _the_questions(
        ask, surface, object_label, shortlist, static_family, strict=strict, severe=severe,
        single_value_votes=single_value_votes, lazy_votes=lazy_votes, merge=merge,
    )
    if ended_with is not None:
        return ended_with

    new = choice is None
    if new:
        choice = _born(db, R, mem, surface, object_label, vec, functional=functional, family=family)
    else:
        logger.info("[vocabulary] %r resolved to existing %r", surface, choice.canonical)
    _count_and_remember(db, V, mem, surface, choice)
    return Relation(
        choice.canonical, choice.is_functional,
        choice.family or family_for(choice.canonical), new=new,
        example=getattr(choice, "example_object", None),
    )


# ---------------------------------------------------------------------------
# The steps of `resolve_predicate`, one function each (5.11.1, 20 Oct 2026,
# the engine's hot spots: the same rules in the same
# order, the same statements and the same questions to the model, nothing
# changed in what a resolution does). Checked by running the function before
# the split and this one on the same 200 scripted sequences of phrases with a
# fake model: same returns, rows, prompts, statements and log lines. A step
# that ends the resolution returns the relation as its last value (`ended_with`),
# else None.
# ---------------------------------------------------------------------------

def _from_the_variant(db, tables: Tables, mem: Memory, surface: str) -> Optional[Relation]:
    """The relation a phrase already learned leads to, else None."""
    V, R = tables.Variant, tables.Relation
    variant = db.query(V).filter(mem.variants, V.surface == surface).first()
    if variant is not None:
        # 5.7.0: the relation a phrase leads to is looked for in the same
        # vocabulary as the phrase (`mem.relations`), never followed by id
        # alone: one vocabulary per end user of a project in korely-agent,
        # and a phrase row pointing into another one (a bug, a hand edit)
        # must not carry that vocabulary's example and vote here (the code
        # audit of 18 Oct 2026).
        rel = (db.query(R).filter(mem.relations, R.id == variant.cluster_id).first()
               if mem.relations is not None else db.get(R, variant.cluster_id))
        if rel is not None:
            return Relation(
                rel.canonical, rel.is_functional,
                rel.family or family_for(rel.canonical),
                example=getattr(rel, "example_object", None),
            )
    return None


def _to_the_present(db, tables: Tables, mem: Memory, surface: str, static_family: str, object_label: str, *,
                 ask: Callable[[str, bool], Optional[str]],
                 embed: Optional[Callable[[str], Optional[list]]], strict: bool, severe: bool,
                 single_value_votes: int, lazy_votes: bool, merge: bool) -> Optional[Relation]:
    """An unseen phrase said in the past, resolved as its present form and
    cached under the phrase as written; None when the present form is the
    phrase itself (or unknown), and the resolution goes on with the phrase."""
    V, R = tables.Variant, tables.Relation
    try:
        present = _vocabulary.ask_and_read(
            ask, _vocabulary.phrase_tense_question(surface, object_label), False,
            _vocabulary.read_phrase_tense, severe=severe)
    except _vocabulary.UnreadableAnswer as e:
        return _unreadable(surface, static_family, e, strict=strict)
    except Exception as e:
        # 3.0.0: `log_safe.error`, here and below: the class and
        # the codes of the error, never its message.
        logger.error("[vocabulary] tense reading failed for %r: %s", surface, _log.error(e))
        if strict:
            raise ResolutionUnavailable(f"could not resolve relation {surface!r}: {_log.error(e)}") from e
        return Relation(surface, None, static_family, resolved=False)
    form = canonicalize_predicate(present) if present else surface
    form = without_the_object(form, object_label)
    if form in ("unknown", surface):
        return None
    rel = resolve_predicate(
        db, tables, mem, form, object_label,
        ask=ask, embed=embed, strict=strict, severe=severe,
        single_value_votes=single_value_votes, lazy_votes=lazy_votes, merge=merge,
    )
    row = (
        db.query(R).filter(mem.relations, R.canonical == rel.canonical).first()
        if rel.resolved and rel.family != "identity" else None
    )
    if row is not None:
        from sqlalchemy.exc import IntegrityError as _Doppione

        try:
            with db.begin_nested():
                db.add(V(surface=surface, cluster_id=row.id, **mem.new_relation))
                db.flush()
        except _Doppione:
            pass  # cached by a concurrent write: same answer
        logger.info("[vocabulary] %r is the past of %r", surface, rel.canonical)
    return Relation(rel.canonical, rel.is_functional, rel.family, resolved=rel.resolved, example=rel.example)


def _the_vector(db, surface: str, static_family: str, object_label: str,
                embed: Optional[Callable[[str], Optional[list]]], *,
                strict: bool) -> Tuple[Any, Optional[Relation]]:
    """The phrase's vector (None without an embedder), and the relation that
    ends the resolution when the call failed on Postgres."""
    text = _vocabulary.text_for_embedding(surface, object_label)
    vec = None
    if embed is not None:
        try:
            vec = embed(text)
        except Exception as e:
            if _pg(db):
                # On Postgres the vector is the shortlist of the relations this
                # phrase could be, and the centroid of a new one. Without it the
                # model is never offered the candidates, and a new relation is
                # cached with no centroid, which no later phrase can ever join:
                # a failed call taken for a decision (ResolutionUnavailable,
                # 2026-09-28). `embed` returning None is "no embedder"
                # (the memory does not merge); raising is a failed call.
                logger.error("[vocabulary] embedding failed for %r: %s", surface, _log.error(e))
                if strict:
                    raise ResolutionUnavailable(f"could not embed relation {surface!r}: {_log.error(e)}") from e
                return vec, Relation(surface, None, static_family, resolved=False)
            # Elsewhere the vector is not read: nothing is decided on it.
            logger.warning("[vocabulary] embedding skipped for %r: %s", surface, _log.error(e))
    return vec, None


def _the_shortlist(db, R: Any, mem: Memory, vec: Any, surface: str, static_family: str, *,
                  strict: bool) -> Tuple[list, Optional[Relation]]:
    """The known relations near the phrase (Postgres, with a vector), and the
    relation that ends the resolution when the query failed."""
    shortlist: list = []
    if vec is not None and _pg(db):
        try:
            # 2.11.0 (an audit of 2.10.0 and the code audit of 6 Oct):
            # in a savepoint, so a failed query does not leave the write's
            # transaction aborted on Postgres (the next statement failed with
            # InFailedSqlTransaction). And a failed shortlist is not an empty
            # one: read as "no known relation is near", the phrase became a
            # new relation, cached for good with its own variant, which no
            # later phrase would ever be offered to join (the reason of
            # `ResolutionUnavailable`). It is a failed resolution, as a failed
            # embedding above is.
            with db.begin_nested():
                shortlist = _candidates(db, R, mem, vec)
        except StaleDataError:
            # A row of this write's own session gone (the memory erased while it
            # is extracted), found by the flush this query triggers: not a failed
            # query. `worker.write_memory` decides, from the database, whether
            # the memory is gone (INFO) or the error is a real one (ERROR).
            raise
        except Exception as e:
            # A database error: its message carries the statement's parameters
            # (3.0.0). The stack without messages (`log_safe.stack`).
            logger.error("[vocabulary] shortlist failed for %r: %s\n%s", surface, _log.error(e), _log.stack(e))
            if strict:
                raise ResolutionUnavailable(
                    f"could not read the relations near {surface!r}: {_log.error(e)}") from e
            return shortlist, Relation(surface, None, static_family, resolved=False)
    return shortlist, None


def _the_questions(ask: Optional[Callable[[str, bool], Optional[str]]], surface: str, object_label: str,
                shortlist: list, static_family: str, *, strict: bool, severe: bool,
                single_value_votes: int, lazy_votes: bool,
                merge: bool) -> Tuple[Any, Optional[bool], Optional[str], Optional[Relation]]:
    """The model's answers: the known relation the phrase is (None: a new
    one), and for a new one its cardinality and family; the relation that ends
    the resolution when an answer is unusable or a call failed."""
    choice = None
    functional = family = None
    try:
        if shortlist and ask is not None and merge:
            options = [(c.canonical, getattr(c, "example_object", None)) for c in shortlist]
            i = _vocabulary.ask_and_read(
                ask, _vocabulary.same_relation_question(surface, object_label, options), False,
                lambda text, severe: _vocabulary.read_same_relation(text, len(shortlist), severe=severe),
                severe=severe)
            choice = shortlist[i] if i is not None else None
        if choice is None:
            functional = None
            family = _schema_family(surface)
            if ask is not None:
                # Kept for this memory for good: decided by majority (I1, 2026-09-28).
                # With `lazy_votes` (3.3.0) asked later, by `decide_if_missing`;
                # the family is asked now either way.
                if not lazy_votes:
                    functional = _vocabulary.decide_single_value(
                        ask, surface, object_label, severe=severe, votes=single_value_votes)
                if family is None:
                    family = _vocabulary.ask_and_read(
                        ask, _vocabulary.family_question(surface, object_label), False,
                        _vocabulary.read_family, severe=severe)
    except _vocabulary.UnreadableAnswer as e:
        # Before any write of this resolution: nothing is cached.
        return choice, functional, family, _unreadable(surface, static_family, e, strict=strict)
    except Exception as e:
        logger.error("[vocabulary] model call failed for %r: %s", surface, _log.error(e))
        if strict:
            raise ResolutionUnavailable(f"could not resolve relation {surface!r}: {_log.error(e)}") from e
        return choice, functional, family, Relation(surface, None, static_family, resolved=False)
    return choice, functional, family, None


def _born(db, R: Any, mem: Memory, surface: str, object_label: str, vec: Any, *,
           functional: Optional[bool], family: Optional[str]) -> Any:
    """The relation of a phrase that is none of the known ones: the row of
    that name when there is one, else a new row."""
    from sqlalchemy.exc import IntegrityError

    choice = db.query(R).filter(mem.relations, R.canonical == surface).first()
    if choice is None:
        extra = {"example_object": (object_label or "")[:500] or None} if hasattr(R, "example_object") else {}
        candidate = R(
            canonical=surface,
            centroid=vec,
            is_functional=functional,
            family=family or "other",
            member_count=0,
            **extra,
            **mem.new_relation,
        )
        try:
            # Two writes resolving the same new phrase at once: the unique
            # index decides, and the loser uses the winner's row.
            with db.begin_nested():
                db.add(candidate)
                db.flush()
            choice = candidate
            logger.info("[vocabulary] new relation %r (functional=%s, family=%s)", surface, functional, family)
        except IntegrityError:
            choice = db.query(R).filter(mem.relations, R.canonical == surface).first()
            if choice is None:
                raise
    return choice


def _count_and_remember(db, V: Any, mem: Memory, surface: str, choice: Any) -> None:
    """One more member for the relation, and the phrase cached as its variant."""
    from sqlalchemy.exc import IntegrityError

    choice.member_count = (choice.member_count or 0) + 1
    try:
        with db.begin_nested():
            db.add(V(surface=surface, cluster_id=choice.id, **mem.new_relation))
            db.flush()
    except IntegrityError:
        pass  # the same surface was cached by a concurrent write: same answer


def reclassify_single_value(
    db,
    tables: Tables,
    mem: Memory,
    *,
    ask: Callable[[str, bool], Optional[str]],
    apply: bool = False,
    severe: bool = True,
) -> Dict[str, list]:
    """Ask again, typed and by majority, whether each relation this memory has
    learned holds one value at a time (I1, 2026-09-28). The answer was asked
    once and kept: a memory whose first answer for `subscribes_to` was "many"
    left every later plan change open, and does not heal by itself.

    With `apply` the new answers are written (the caller commits); without,
    nothing changes and the result says what would. A relation whose model
    call fails or whose answer cannot be read is left as it is and listed
    under "skipped". Returns {"changed": [(canonical, old, new)],
    "same": [canonical], "skipped": [canonical]}. A relation stored with
    no example value is left as it is ("same"): see the loop."""
    R = tables.Relation
    outcome: Dict[str, list] = {"changed": [], "same": [], "skipped": []}
    if R is None:
        return outcome
    for rel in db.query(R).filter(mem.relations).order_by(R.canonical).all():
        example = (getattr(rel, "example_object", None) or "").strip()
        if not example:
            # Without the value it was seen with, the only question left is the
            # general one, the very one that answered "many" for plans: asking
            # it again could only move a relation back to the wrong answer.
            outcome["same"].append(rel.canonical)
            continue
        try:
            new = _vocabulary.decide_single_value(ask, rel.canonical, example, severe=severe)
        except Exception as e:  # noqa: BLE001 - one relation never stops the others
            logger.warning("[vocabulary] reclassification of %r skipped: %s", rel.canonical, _log.error(e))
            outcome["skipped"].append(rel.canonical)
            continue
        if new is None or new == rel.is_functional:
            outcome["same"].append(rel.canonical)
            continue
        outcome["changed"].append((rel.canonical, rel.is_functional, new))
        if apply:
            rel.is_functional = new
    if apply and outcome["changed"]:
        db.flush()
    return outcome


def review_the_variants(
    db,
    tables: Tables,
    mem: Memory,
    *,
    ask: Callable[[str, bool], Optional[str]],
    embed: Optional[Callable[[str], Optional[list]]] = None,
    apply: bool = False,
    severe: bool = True,
    single_value_votes: int = 1,
    lazy_votes: bool = False,
    facts: Any = None,
    votes: int = 1,
) -> Dict[str, list]:
    """Ask again, one pair at a time, whether each phrase this memory learned
    as a variant of a relation is that relation (3.3.0, 8 Oct, "the review of
    the vocabulary already learned").

    A phrase is merged at birth by picking among the nearest known relations
    (`same_relation_question` with the shortlist), and the answer is kept
    for good. A bench (LoCoMo, gpt-oss-120b writer, 2,632 variants,
    1,423 under another name) shows merges that change what a fact asserts:
    "visited" in `plans_to_visit` (BUG-365, 53 variants), "met" and "friend_of"
    in `networking_with`, "mentors" in `receives_support_from` (the direction
    reversed), "dyed_hair" in `practices`. Here the same question is asked
    with ONE option, the relation the phrase sits in, and each side carries
    its own example value: the phrase's from its latest fact, the relation's
    from `example_object`. Two phrases name the same relation when one can
    replace the other without changing what is asserted (the question's own
    test; paraphrase as mutual entailment, Androutsopoulos and Malakasiotis,
    JAIR 38, 2010, §1).

    Not asked: a phrase whose facts are all in the past (it sits in the
    relation of its present form on purpose, `phrase_tense_question`:
    "visited" under `visits` is right) is listed under "to_the_past".

    `votes` (8 Oct, a dry run on LoCoMo: about one separation in ten
    was two names of one relation, near-synonyms or two forms of the same
    phrase): a separation is kept for good, so "different" needs a majority
    of `votes` answers, asked with thinking after the first (self-consistency,
    Wang et al., ICLR 2023, as `vocabulary.decide_single_value`). The first
    "same" ends the asking: the phrase stays where it was, the reading of
    before, at one call. 1: one answer decides, as before. Measured on 8 Oct
    (a review of LoCoMo conversations 0-4): 329 separations with the
    votes against 337 with one answer, so they take back about one in forty;
    they stay because a separation is kept for good and the extra calls are
    only for the phrases a first answer would separate.

    With `apply`, a phrase the model separates leaves the relation, inside
    a savepoint: its variant row goes, it is resolved again under its own
    name (`resolve_predicate(merge=False)`: a relation of that name, or a new
    one with its family and cardinality asked as at any birth), and the facts
    written with it move there (`predicate_canonical`, and `predicate_family`
    when the table has it). A closure between the two groups (a fact of the
    phrase closed by a fact of the rest of the relation, or the other way
    round) is not reopened: it is listed under "closings_to_reread", for a
    reader, because the judge may have been right for its own reasons. A phrase
    whose question or new birth fails is left where it was ("skipped"). The
    caller commits.

    Returns {"separated": [(phrase, relation, facts)], "same": [(phrase,
    relation)], "to_the_past": [(phrase, relation)], "skipped": [(phrase,
    relation)], "closings_to_reread": [(closed fact id, closing fact id)]}.

    `facts`: the SQL condition of every fact that uses this vocabulary, when
    it is wider than `mem.facts` (Agents: the vocabulary is the end user's,
    in one project, a memory also the agent namespace's); the facts of a
    separated phrase move wherever they are."""
    R, V, F = tables.Relation, tables.Variant, tables.Fact
    where = facts if facts is not None else mem.facts
    outcome: Dict[str, list] = {"separated": [], "same": [], "to_the_past": [], "skipped": [],
                              "closings_to_reread": []}
    if R is None or V is None:
        return outcome
    facts_of: Dict[str, list] = {}
    for variant, rel in _the_variants(db, V, R, mem):
        if rel.canonical not in facts_of:
            facts_of[rel.canonical] = _facts_of_the_relation(db, F, where, rel.canonical)
        facts = facts_of[rel.canonical]
        own = [f for f in facts
                if canonicalize_predicate(f.predicate_raw or "") == variant.surface]
        pair = (variant.surface, rel.canonical)
        if own and all((getattr(f, "tense", None) or "current") == "past" for f in own):
            outcome["to_the_past"].append(pair)
            continue
        example = (own[0].object_label or "") if own else ""
        question = _vocabulary.same_relation_question(variant.surface, example,
                                                [(rel.canonical, getattr(rel, "example_object", None))])
        try:
            different = _are_different(ask, question, votes=votes, severe=severe)
        except Exception as e:  # noqa: BLE001 - one phrase never stops the others
            logger.warning("[vocabulary] review of a variant of %r skipped: %s", rel.canonical, _log.error(e))
            outcome["skipped"].append(pair)
            continue
        if not different:
            outcome["same"].append(pair)
            continue
        if not apply:
            outcome["separated"].append((variant.surface, rel.canonical, len(own)))
            continue
        try:
            _separate(db, tables, mem, variant, rel, example, own, ask=ask, embed=embed,
                    severe=severe, single_value_votes=single_value_votes, lazy_votes=lazy_votes)
        except Exception as e:  # noqa: BLE001 - rolled back to the savepoint: the variant stays
            logger.warning("[vocabulary] separation of a variant of %r skipped: %s", rel.canonical, _log.error(e))
            outcome["skipped"].append(pair)
            continue
        outcome["separated"].append((variant.surface, rel.canonical, len(own)))
        others, to_reread = _closures_between_the_groups(own, facts)
        outcome["closings_to_reread"].extend(to_reread)
        facts_of[rel.canonical] = others
    return outcome


# ---------------------------------------------------------------------------
# The steps of `review_the_variants`, one function each (5.11.1, 20 Oct 2026,
# the engine's hot spots: the same questions in the same
# order, the same statements, nothing changed in what a review does). Checked
# by running the function before the split and this one on the same 150
# scripted vocabularies with a fake model: same returns, rows, prompts,
# statements and log lines.
# ---------------------------------------------------------------------------

def _the_variants(db, V: Any, R: Any, mem: Memory) -> list:
    """Every phrase of this vocabulary learned under another relation's name,
    with that relation."""
    return (
        db.query(V, R).join(R, R.id == V.cluster_id)
        .filter(mem.variants, mem.relations, V.surface != R.canonical)
        .order_by(R.canonical, V.surface).all()
    )


def _facts_of_the_relation(db, F: Any, where: Any, canonical: str) -> list:
    """The facts written with a relation, newest first."""
    return (
        db.query(F).filter(where, F.predicate_canonical == canonical)
        .order_by(F.created_at.desc()).all()
    )


def _are_different(ask: Callable[[str, bool], Optional[str]], question: str, *,
                  votes: int, severe: bool) -> bool:
    """Whether a majority of `votes` answers says the phrase is not the
    relation; the first "same" ends the asking. Raises when a call fails or an
    answer cannot be read."""
    majority = max(1, votes) // 2 + 1
    different = equal = 0
    for i in range(max(1, votes)):
        choice = _vocabulary.ask_and_read(
            ask, question, i > 0,
            lambda text, severe: _vocabulary.read_same_relation(text, 1, severe=severe),
            severe=severe,
        )
        if choice == 0:
            equal += 1
            if i == 0 or equal >= majority:
                break
        else:
            different += 1
            if different >= majority:
                break
    return different >= majority


def _separate(db, tables: Tables, mem: Memory, variant: Any, rel: Any, example: str, own: list, *,
            ask: Callable[[str, bool], Optional[str]],
            embed: Optional[Callable[[str], Optional[list]]], severe: bool,
            single_value_votes: int, lazy_votes: bool) -> None:
    """The phrase leaves the relation, inside a savepoint: its variant row
    goes, it is resolved again under its own name, and its facts move there.
    Raises (rolled back to the savepoint) when the new birth fails."""
    with db.begin_nested():
        db.delete(variant)
        rel.member_count = max(0, (rel.member_count or 1) - 1)
        db.flush()
        new = resolve_predicate(
            db, tables, mem, variant.surface, example,
            ask=ask, embed=embed, strict=True, severe=severe,
            single_value_votes=single_value_votes, lazy_votes=lazy_votes,
            merge=False,
        )
        for f in own:
            f.predicate_canonical = new.canonical
            if hasattr(f, "predicate_family"):
                f.predicate_family = new.family
        db.flush()


def _closures_between_the_groups(own: list, facts: list) -> Tuple[list, list]:
    """The facts left in the relation, and the closures between the phrase's
    facts and theirs, either way round (closed id, closing id), for a reader."""
    to_reread: list = []
    own_ids = {f.id for f in own}
    others = [f for f in facts if f.id not in own_ids]
    other_ids = {f.id for f in others}
    for f in own:
        if getattr(f, "invalidated_by_id", None) in other_ids:
            to_reread.append((f.id, f.invalidated_by_id))
    for f in others:
        if getattr(f, "invalidated_by_id", None) in own_ids:
            to_reread.append((f.id, f.invalidated_by_id))
    return others, to_reread


def examples_after_the_erasure(
    db,
    tables: Tables,
    relations: Any,
    facts_of_the_relation: Callable[[Any], Any],
    *,
    embed: Optional[Callable[[str], Optional[list]]] = None,
) -> int:
    """After an erasure (GDPR Art. 17), the vocabulary keeps no value of whom
    it erased (2.11.0, code audit of 6 Oct). Call it in the erasure's
    transaction, after the facts are deleted. Returns how many relations
    changed their example.

    A learned relation keeps `example_object`, the object of the first fact
    it was learned from, and `centroid`, the vector of the relation's phrase
    WITH that object (`vocabulary.text_for_embedding`). The example enters
    the prompts written for the vocabulary's later facts (Agents: one end
    user's, since 18 Oct 2026; the project's, and the account's before, so
    every other end user's): "has_condition (as in: ... type 2 diabetes)" when a phrase is
    resolved, "[single-valued for values like: ...]" in the judge's lines
    (typed functionality, Lin, Mausam, Etzioni, EMNLP 2010 §3, the reason the
    example exists). So the first end user's value outlived their erasure,
    and went on into the prompts of the others. The centroid is a copy of
    the same text: a text embedding gives its text back to a model trained to
    invert it (Morris, Kuleshov, Shmatikov, Rush, "Text Embeddings Reveal
    (Almost) As Much As Text", EMNLP 2023: 92% of 32-token inputs recovered
    exactly, and full names recovered from the embeddings of clinical notes).

    The rule keeps the vocabulary working instead of emptying it: an example
    stays while a fact of the relation's memory still states that value
    (`facts_of_the_relation(rel)`, the engine's condition on its fact table:
    Agents, the account, the relation's project and its end user), and a value somebody
    else still holds is nobody's erased data. Otherwise the example becomes
    the object of the oldest fact of that relation still in the store (the
    first one written, as the original example was), or nothing when the
    relation has no fact left; the centroid is computed again from the
    relation and its new example, exactly as `resolve_predicate` computes the
    centroid of a new relation, so the shortlist offers it as before. Without
    `embed`, or when it fails, the centroid goes (NULL): the erased value
    must not stay in it, and a relation without a centroid is only not
    offered to a NEW phrase (its own phrases still resolve to it; the cost is
    a duplicate relation, never a wrong merge, as `same_relation_question`
    weighs it). The relation's name and its phrases stay: they are the
    vocabulary's words, not an end user's values.

    Ordered against whoever fills a centroid meanwhile (the code audit of 20
    Oct 2026: a vector drain that committed inside the erasure kept
    the vector of the erased value). The vector drain and `manage
    recompute-vectors` write a centroid with a compare-and-set on the example
    they embedded (services/predicates._store_centroid), which only sees
    what is committed: the erased example, until this transaction commits.
    So, in three steps:

    1. a read without locks finds the relations that change, and computes
       their new example and its vector: the slow embedder call holds no
       lock on the vocabulary;
    2. those rows only, the ones this function writes, are locked (FOR NO
       KEY UPDATE, the lock their UPDATE takes anyway, in id order) and read
       again, and their new example is decided again under the lock; the
       vector of step 1 is kept only if it was made from that same example,
       otherwise the centroid goes (NULL) and the drain, which fills a NULL
       centroid from the relation's example, computes it from the new one;
    3. both columns are written explicitly (`flag_modified`): a NULL over a
       NULL that was loaded is written too, so a centroid committed by the
       drain between step 1 and step 2 does not survive.

    A drain that commits before step 2 is overwritten by step 3; one whose
    UPDATE comes after step 2 waits for this transaction, and under READ
    COMMITTED Postgres evaluates its condition again on the row this
    transaction committed (PostgreSQL documentation, "Transaction Isolation",
    §13.2.1 Read Committed Isolation Level), whose example is no longer the
    one it embedded: it writes nothing."""
    R, F = tables.Relation, tables.Fact
    if R is None or not hasattr(R, "example_object"):
        return 0
    from sqlalchemy.orm.attributes import flag_modified

    # 1. Unlocked: which relations change, to what, and the vector of it.
    vector_search: Dict[Any, Tuple[Optional[str], Optional[list]]] = {}
    for rel in db.query(R).filter(relations, R.example_object.isnot(None)).all():
        changes, example = _example_after_the_erasure(db, F, rel, facts_of_the_relation(rel))
        if not changes:
            continue
        vector = None
        if embed is not None:
            try:
                vector = embed(_vocabulary.text_for_embedding(rel.canonical, example or ""))
            except Exception as e:  # noqa: BLE001 - the erasure never waits for a vector
                logger.warning("[vocabulary] centroid of %r not recomputed after an erasure, cleared: %s",
                               rel.canonical, _log.error(e))
                vector = None
        vector_search[rel.id] = (example, vector)
    if not vector_search:
        return 0
    # 2. Locked, read again, decided again. The session's pending changes go
    # out first: `populate_existing` would otherwise overwrite them.
    db.flush()
    locked = (db.query(R).filter(R.id.in_(list(vector_search)))
                .order_by(R.id).with_for_update(key_share=True).populate_existing().all())
    changed = 0
    for rel in locked:
        if rel.example_object is None:
            continue
        changes, example = _example_after_the_erasure(db, F, rel, facts_of_the_relation(rel))
        if not changes:
            continue
        example_read, vector = vector_search[rel.id]
        if example_read != example:
            vector = None
        # 3. Written whatever the session thinks the row held.
        rel.example_object = example
        rel.centroid = vector
        flag_modified(rel, "example_object")
        flag_modified(rel, "centroid")
        changed += 1
    if changed:
        db.flush()
        logger.info("[vocabulary] %d relation example(s) replaced after an erasure", changed)
    return changed


def _example_after_the_erasure(db, F, rel, where) -> Tuple[bool, Optional[str]]:
    """(whether `rel` loses its example, its new example), by the rule of
    `examples_after_the_erasure`: kept while a fact of the relation's
    memory (`where`) still states it, otherwise the object of the oldest fact
    of the relation still in the store, or None."""
    keep_grounded = db.query(F.id).filter(where, F.object_label == rel.example_object).first()
    if keep_grounded is not None:
        return False, rel.example_object
    first = (
        db.query(F.object_label)
        .filter(where, F.predicate_canonical == rel.canonical)
        .order_by(F.created_at.asc(), F.id.asc())
        .first()
    )
    return True, (((first[0] or "")[:500] or None) if first is not None else None)


def _unreadable(surface: str, static_family: str, e: Exception, *, strict: bool) -> Relation:
    """An unusable answer (`severe`): raise, or the surface form uncached.
    The message of `UnreadableAnswer` describes the answer by its shape
    (`vocabulary._excerpt`, 3.0.0), so it may go to the log as it is."""
    logger.error("[vocabulary] unusable answer for %r: %s", surface, e)
    if strict:
        raise ResolutionUnusable(f"unusable answer resolving relation {surface!r}: {e}") from e
    return Relation(surface, None, static_family, resolved=False)


def family_filter(F, family: str):
    """SQL condition "this fact belongs to `family`", the same rule as
    `families.family_of_the_fact`: the family stored on the fact wins; a fact
    without one (written before BUG-109) falls back to the static map."""
    from sqlalchemy import and_, false, or_

    from .families import predicates_in_family

    fam = (family or "").strip().lower()
    if fam == "other":
        mapped = list(PREDICATE_TO_FAMILY)
        return or_(
            F.predicate_family == "other",
            and_(F.predicate_family.is_(None), ~F.predicate_canonical.in_(mapped)),
        )
    preds = predicates_in_family(fam)
    static = and_(F.predicate_family.is_(None), F.predicate_canonical.in_(preds)) if preds else false()
    return or_(F.predicate_family == fam, static)


def decide_if_missing(
    db,
    tables: Tables,
    mem: Memory,
    canonical: str,
    ask: Optional[Callable[[str, bool], Optional[str]]],
    *,
    severe: bool = False,
    votes: int = 1,
    examples: Sequence[str] = (),
) -> Tuple[bool, Optional[str]]:
    """The relation's tag, voted now when it is still undecided (3.3.0,
    `resolve_predicate(lazy_votes=True)`), with the same question and the
    same example as at its birth (`example_object`, the object it was born
    with): the answer is the same one, asked when a judge needs it instead of
    when the relation was born. A reading of its arm fn (7 Oct): the
    vocabulary costs 1.15 calls per memory, and 570 of 1,210 relations are
    used by one fact only. Kept for good, as at birth; undecided again (no
    majority) stays None and is asked at the next meeting. A relation born
    with the switch on and never met stays undecided if the switch is turned
    off: `reclassify_single_value` decides it then.

    Strict as the resolution: a call that fails raises `ResolutionUnavailable`,
    an answer the question does not allow raises `ResolutionUnusable`; the
    write is retried, nothing is stored. Returns `single_value` after.

    `examples` (8 Oct, the confirmation run of 3.3.0): the values actually in
    conflict, the new fact's object and the earlier fact's, which the judge
    has in hand at the first meeting. The vote is asked typed by each of them
    instead of by the birth example, and the relation holds one value only
    when it does for every one of them. Typed functionality reads the kind
    from the arguments (Lin, Mausam, Etzioni, EMNLP 2010 §3); a birth example
    can be degenerate: an arm of that run learned `reads` from "reads
    reading", voted one value typed by "reading", and every book and film
    then closed the one before (17 closures in two conversations). All of
    them, not a majority: the tag is what leads the judge to close, the
    direction that loses information, and the vocabulary already prefers
    the one that loses nothing (`same_relation_question`: "Prefer null
    when unsure"). A value with no majority leaves the tag undecided, asked
    again at the next meeting. Without `examples`, the birth example as before."""
    R = tables.Relation
    if R is not None and ask is not None:
        rel = db.query(R).filter(mem.relations, R.canonical == canonical).first()
        # Only a relation born with its example: an old one without it keeps
        # its tag undecided, as `reclassify_single_value` leaves it (the
        # general question, without a value, is the one it refuses to ask).
        values = [v for v in dict.fromkeys((e or "").strip() for e in examples) if v]
        if not values and rel is not None and getattr(rel, "example_object", None):
            values = [rel.example_object]
        if rel is not None and rel.is_functional is None and values:
            try:
                answers = [_vocabulary.decide_single_value(ask, canonical, v, severe=severe, votes=votes)
                            for v in values]
                rel.is_functional = None if any(r is None for r in answers) else all(answers)
            except _vocabulary.UnreadableAnswer as e:
                logger.error("[vocabulary] unusable answer on the cardinality of %r: %s", canonical, e)
                raise ResolutionUnusable(f"unusable answer on the cardinality of {canonical!r}: {e}") from e
            except Exception as e:
                logger.error("[vocabulary] cardinality of %r not decided: %s", canonical, _log.error(e))
                raise ResolutionUnavailable(f"could not decide the cardinality of {canonical!r}: "
                                            f"{_log.error(e)}") from e
            logger.info("[vocabulary] %r decided when first needed (functional=%s)", canonical, rel.is_functional)
            db.flush()
    return single_value(db, tables, mem, canonical)


def single_value(db, tables: Tables, mem: Memory, canonical: str) -> Tuple[bool, Optional[str]]:
    """(holds one value, the object that was measured on). Typed functionality
    (Lin, Mausam, Etzioni, EMNLP 2010 §3): "published in" holds one value when
    the value is a year and many when it is a language, so the judge is told
    WHICH kind of value the cardinality was measured on and compares the kinds
    itself. The example is None for relations learned before it was stored."""
    R = tables.Relation
    if R is None:
        return False, None
    columns = [R.is_functional]
    if hasattr(R, "example_object"):
        columns.append(R.example_object)
    row = db.query(*columns).filter(mem.relations, R.canonical == canonical).first()
    if not row or not row[0]:
        return False, None
    return True, (row[1] if len(row) > 1 else None)
