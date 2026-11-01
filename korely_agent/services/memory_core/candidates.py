"""Which earlier facts the contradiction judge reads (memory_core, piece 2 of
the shared-code plan, 2026-09-28).

Until today the two Agents engines had this function twice, line for line:
`agent_facts/contradiction._stage1_candidates` in the Cloud and in
korely-agent. The copies had already diverged once in a way that mattered: the
semantic stage built its own chain of filters and left the project out, so a
fact written in project Beta closed a fact of project Alfa. It was fixed by
hand in both. Here it is written once; the engine passes only WHO may be a
candidate (`scope`, a SQL condition on its fact table), and every query below
goes through that one condition.

Recall here, precision in the judge (ConflictBank, Su et al. 2024): a
pre-filter that lets through anything plausibly about the same thing, and a
model that decides. A candidate kept costs one line in a prompt; a candidate
missed is a contradiction nobody ever notices.

The stages, in the order the judge reads them (most precise signal first):
  1a   same canonical subject, same relation or its opposite family;
  1c   same subject, a compatible object, another relation (BUG-111);
  1d   the same two entities with the relation the other way round;
  1a-bis  same relation family inside one end user, any subject surface
       (entity resolution within a bounded scope, Graphiti §2.2);
  1b   nearest facts by vector (pgvector only).

Dependency: SQLAlchemy 2 only, like `store`.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from sqlalchemy import func

from . import log_safe as _log
from . import thresholds
from .store import (Tables, _pg, apart_from, as_before_the_write, outside_the_source,
                    without_the_same_source)
from .contradiction import arguments_swapped, compatible_objects, merge_candidates
# `cosine` lives in `vector_search` since 2.12.0; the copy of its name that stayed
# here for the code importing it from this module left with 4.0.0 (nobody did).
from .vector_search import nearest

logger = logging.getLogger(__name__)

# Stage 1b: cosine distance cutoff and cap. Recall-y by design, the judge is
# the precision gate (ConflictBank, Su et al. 2024). The cutoff depends on the
# embedding model (2.4.0): the one in use is `thresholds.current().semantic`.
# (SEMANTICA_MAX_DIST, Gemini's value kept for code that imported it, left
# with 5.2.4: nobody did.)
SEMANTIC_LIMIT = 10

# There was an object filter on Stage 1a here, in the self-hosted engine only:
# a structural candidate farther than 0.20 from the new fact was dropped before
# the judge (measured on five pairs, korely-agent cc02629, 2026-09-10). Measured
# again on 2026-09-28 (the shared-code plan, "the three differences"): a
# real replacement falls beyond it
# ("Globex uses Jira" -> "uses Linear", 0.217), unrelated objects under the
# same subject and relation span 0.125-0.233, so the cut separates nothing, and
# on the support-desk corpus it saved no question to the judge. Removed from
# both engines. A candidate kept costs one line in a prompt; a candidate
# dropped is a contradiction nobody ever notices.


class CandidatesUnread(RuntimeError):
    """A stage of the candidate search could not run (2.11.0): the write is
    not judged on the candidates the other stages found. Not "no candidates":
    a contradiction the judge never sees stays open next to the fact it
    contradicts, and nobody asks again (the same reason a failed judge is not
    a verdict, `judge.JudgementMissing`). The engine's write rolls back, and
    its failure handling decides what follows."""


def for_the_judge(
    db,
    tables: Tables,
    scope: Any,
    *,
    exclude_id: Any,
    subject_label: str,
    predicate_canonical: str,
    subject_canonical: Optional[str] = None,
    object_canonical: Optional[str] = None,
    is_open: Any = None,
    source: Any = None,
    with_recall: bool = False,
    vector=None,
    limit: int = 10,
    only_arguments: bool = False,
) -> List[Any]:
    """The earlier facts the judge must weigh against a new one.

    `scope`        who may be a candidate (owner, project, end user): the wall.
    `is_open`      what "still true" means (Agents: `true_at()`, which also
                   counts a fact closed on a future date); default
                   `invalid_at IS NULL`.
    `source`       the memory being read (BUG-123): the facts it stated, born
                   from it or reconfirmed by it, never enter. A memory listing
                   "hobby, developer, team" does not close its own plans (rule
                   3 of `contradiction`, Graphiti §2.2.3).
    `with_recall`  Stage 1a-bis on: only when the scope is one end user, where
                   the same relation family is almost always the same entity;
                   the global scope is too broad to assume it.
    `only_arguments`  a past-tense statement closes its own twin by itself;
               what it can still make false are the facts about the same two
               entities (stages 1c and 1d only).
    """
    F = tables.Fact
    # 6.2.0: the copies an edited memory's previous text shared, weighed by
    # the write's judgement as another memory's facts (`store.judged_apart`).
    apart = apart_from(db, source)
    _scope = _the_wall(db, tables, scope, is_open=is_open, exclude_id=exclude_id, source=source,
                       apart=apart)

    def _base():
        # 3.3.0: the same relation only. Two relations that exclude each
        # other ("likes X" / "dislikes X") share their arguments, and stage
        # 1c below brings them, with no list of opposing pairs.
        return _scope(db.query(F)).filter(F.predicate_canonical == predicate_canonical)

    _sc = func.lower(func.coalesce(F.subject_canonical, F.subject_label))
    sc_new = (subject_canonical or subject_label or "").strip().lower()
    oc_new = (object_canonical or "").strip().lower()
    # In SQL the names meet with the database's lower() on both sides (2.11.0,
    # as `store.find_open`): Python's lower() is another function, and
    # on "İstanbul" the subject stages found nothing. The Python-lowered forms
    # stay for the comparisons made in Python below.
    sc_sql = func.lower((subject_canonical or subject_label or "").strip())
    oc_sql = func.lower((object_canonical or "").strip())

    same, inverse = _same_arguments(db, F, _scope, _sc, predicate_canonical,
                                        sc_new=sc_new, oc_new=oc_new, sc_sql=sc_sql, oc_sql=oc_sql)
    if only_arguments:
        return without_the_same_source(tables, merge_candidates(same, inverse), source, apart=apart)

    # 1a: exact subject, on the CANONICAL form (after the aliases) and
    # case-insensitive: "Globex" must see the facts written when it was "Acme".
    structural = (
        _base()
        .filter(_sc == sc_sql)
        .order_by(*_newest_first(F))
        .limit(limit)
        .all()
    )
    # 1a-bis: the extractor writes one entity as "Maria", "the user", "the
    # runner"; the exact-subject stage misses those updates, and the old value
    # is never superseded. Independent of the embedding, so it also covers the
    # case where 1b is blind.
    recall: List[Any] = []
    if with_recall:
        recall = _base().order_by(*_newest_first(F)).limit(limit).all()

    semantic = _semantic(db, F, _scope, vector)

    return without_the_same_source(
        tables, merge_candidates(structural, same, inverse, recall, semantic), source, apart=apart,
    )


# ---------------------------------------------------------------------------
# The steps of `for_the_judge`, one function each (5.11.1, 20 Oct 2026, the
# engine's hot spots: the same stages in the same order,
# the same statements, nothing changed in what the judge reads). Checked by
# running the function before the split and this one on the same 200
# scripted memories: same candidates, same statements.
# ---------------------------------------------------------------------------

def _the_wall(db, tables: Tables, scope: Any, *, is_open: Any, exclude_id: Any, source: Any,
              apart: Any = ()):
    """Who may enter, whatever shape the query has (with or without the
    distance column). Every query of `for_the_judge` is wrapped by this, the
    lesson of the project wall that the semantic stage once left open."""
    F = tables.Fact

    # 3.1.3: still true as when the running write began, so a fact an earlier
    # statement of the same write closed is still weighed
    # (`store.one_write`, Graphiti's episode against the graph before it).
    truth = as_before_the_write(db, F, is_open if is_open is not None else F.invalid_at.is_(None))

    def _scope(q):
        return (
            q.filter(scope)
            .filter(truth)
            .filter(F.id != exclude_id)
            # BUG-123: in the scope, so every stage (and its LIMIT) sees it.
            .filter(outside_the_source(tables, source, apart=apart))
        )

    return _scope


def _newest_first(F: Any) -> tuple:
    """Newest first, then by id (5.2.7). The facts of one write share their
    created_at (the transaction's now()), so a LIMIT on created_at alone cut
    inside a tie in an order Postgres does not define: on the kept LoCoMo
    database 13 of the 40 subjects with more than 10 open facts had the 10th
    and 11th at the same instant, on LongMemEval 200 of 386 (measured on
    18 Oct 2026). Which candidates the judge saw could change from one
    reading to the next. The id makes the cut the same every time; it does
    not make it better (a code review)."""
    return (F.created_at.desc(), F.id.desc())


def _same_arguments(db, F: Any, _scope, _sc: Any, predicate_canonical: str, *,
                      sc_new: str, oc_new: str, sc_sql: Any, oc_sql: Any) -> Tuple[List[Any], List[Any]]:
    """Stages 1c and 1d, the facts about the same two entities; none without
    an object."""
    # 1c (BUG-111): same subject, a compatible object ("SSO" / "SSO support"),
    # another relation ("lacks X" / "adopted X").
    same: List[Any] = []
    # 1d: the relation the other way round ("Salesforce serves_as_crm_for
    # Globex" when the new fact is "Globex no longer uses Salesforce").
    inverse: List[Any] = []
    if oc_new:
        same = [
            c for c in (
                _scope(db.query(F))
                .filter(_sc == sc_sql, F.predicate_canonical != predicate_canonical)
                .order_by(*_newest_first(F))
                .limit(50)
                .all()
            )
            if compatible_objects(oc_new, c.object_canonical or c.object_label)
        ][:5]
        inverse = [
            c for c in (
                _scope(db.query(F))
                .filter(_sc == oc_sql)
                .order_by(*_newest_first(F))
                .limit(50)
                .all()
            )
            if arguments_swapped(sc_new, oc_new, c.subject_canonical or c.subject_label,
                                   c.object_canonical or c.object_label)
        ][:5]
    return same, inverse


def _semantic(db, F: Any, _scope, vector) -> List[Any]:
    """Stage 1b: facts about the same thing that the extractor canonicalized
    differently ("charity 5K run | personal_best_time" vs "<user> |
    has_personal_best_time"). cosine_distance is pgvector-only; on SQLite the
    query not only errors but poisons the session, so it is gated by dialect
    instead of run-and-catch."""
    semantic: List[Any] = []
    if vector is not None and _pg(db):
        threshold = thresholds.current().semantic
        try:
            # 2.11.0 (an audit of 2.10.0 and the code audit of 6 Oct):
            # in a savepoint. On Postgres a failed statement (a vector of
            # another size, a statement timeout) aborts the transaction, and
            # the `except` that said "best effort" left it aborted: the next
            # statement of the write failed with InFailedSqlTransaction
            # (reproduced). The savepoint rolls back this query
            # alone, as BUG-300 does for the context (agent_context.py).
            # 2.12.0 (BUG-320): `vector_search.nearest` sets the depth of
            # the HNSW scan first, inside this savepoint: the index holds
            # every account's facts and the memory's filters come after it,
            # so 10 asked could be fewer, or none.
            with db.begin_nested():
                rows = nearest(db, _scope(db.query(F)).filter(F.embedding.isnot(None)),
                               F.embedding, vector, SEMANTIC_LIMIT)
        except Exception as e:
            # And the write does not go on without it. It used to: "never
            # break a write on the semantic pre-filter" judged the new fact on
            # the other stages alone, so a contradiction only 1b finds stayed
            # open for good, the memory marked done. A stage that could not
            # run is not "no candidates" (`CandidatesUnread`): recall here,
            # precision in the judge (ConflictBank, Su et al. 2024), and a
            # candidate missed is a contradiction nobody ever notices.
            # 3.0.0: a database error's message carries the
            # statement's parameters (the end user's id among them): its
            # classes, codes and frames only (`log_safe`).
            logger.error("[memory/F3] semantic candidate retrieval failed, the write is not judged "
                         "without it: %s\n%s", _log.error(e), _log.stack(e))
            raise CandidatesUnread(f"semantic candidate retrieval failed: {_log.error(e)}") from e
        for cand, d in rows:
            if d is not None and float(d) <= threshold:
                semantic.append(cand)
    return semantic
