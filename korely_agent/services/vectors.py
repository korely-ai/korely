"""A vector that did not come back is retried, a bounded number of times,
and then said (2026-09-28).

A memory or a fact is found by meaning only through its vector: memory search
and the ranking of facts in /v1/context read the rows that have one, and so
does the semantic half of the contradiction check. What a failed call did
until today:

  * a memory was stored without one, and "a future worker can backfill" it
    said; there was none, and `manage reextract` re-queued facts only, so the
    memory stayed out of similarity search for good;
  * an update whose call failed kept the OLD vector, marked done, on the new
    text: the memory said one thing and was found by what it no longer said;
  * a fact write with a key set was refused (the round of the same morning),
    where the hosted engine keeps the fact and retries its vector.

Now the write keeps the row and leaves it waiting, and the worker computes the
vector later. The row is the job, the way `graph_sync_status` is the job of
the facts drain:

    embedding_status    pending   no vector for the current text yet
                        done      the vector of the current text
                        error     ATTEMPTS calls gave none; the drain stops.
                                  `manage reextract` queues it again, and so
                                  does a new text (PATCH of a memory)
                        stale     a vector from the embedding model before;
                                  `manage recompute-vectors` replaces it
                                  (the end of this file)
    embedding_attempts  the calls made for the current text
    embedding_next_at   when the next one may be made (NULL: at the next pass)

The ladder: the first retry a minute after the failed call, each wait twice
the one before up to six hours, ATTEMPTS calls in all (the one on the write
included), about twenty hours from first to last: the same day of an
unreachable provider that the hosted engine's brake covers. A row that fails
on every call costs ATTEMPTS embedding calls, then waits for somebody.

The drain is also the safety net. It reads the rows, not a queue, so what no
write path scheduled is found too: the rows left `pending` before this change
(a memory written with no key, a call that failed when failures were
dropped), and whatever a future write path forgets to schedule. An attempt is
counted and the next one scheduled when a row is CLAIMED, before the call, so
a process that dies in the middle loses nothing: the row comes back when its
wait is over, with the attempt spent.

No transaction is held during a call. The claim commits, the model is asked,
and the vector is written as a compare-and-set on the row as it was claimed
(still waiting, same attempt, and for a memory still live and saying the text
that was embedded): an update, a forget or another replica in between wins,
and the late vector is dropped rather than put on the wrong text.

With no Google key there is no embedder and nothing to do: the rows stay
`pending`, which is true, and the day a key is set the drain embeds them, a
few per pass (`vector_drain_limit`).
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db.models import AgentFact, AgentMemory
from . import llm_usage, regions
# The writers and the judge this drain calls, imported at the top since the
# audit of 17 Oct 2026: they no longer import this module (vector_state.py),
# so there is no circle to hold with imports inside functions. Called through
# their modules, so a vector computed here is the one the writer computes
# now, whatever replaced it (the tests do).
from . import memory_engine as _memory_engine
from .facts import contradiction
from .facts import service as _facts
from .facts.temporal import true_at
from .ids import fact_public_id
from .memory_core import log_safe
from .memory_core.store import ResolutionUnavailable
from .memory_core.candidates import CandidatesUnread
from .memory_core.vector_search import cosine

logger = logging.getLogger(__name__)

# The row's vector job (its states, the ladder, the write's changes) lives
# in vector_state.py, below the writers this drain calls, and is read from
# here under its old names (`vectors.DONE`, `vectors.requeue_given_up`).
from .vector_state import (  # noqa: F401 - the public names
    ATTEMPTS, DONE, ERROR, FAILURES_BEFORE_STOPPING, PENDING,
    STALE, utc_now, pause_after, requeue_given_up,
)


# ── the drain ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Kind:
    """What differs between a memory and a fact, and nothing else."""

    name: str
    model: type
    columns: tuple
    live: tuple
    # (db, row) -> the text the vector is computed on: a memory's text, a
    # fact's sentence, the same as the write computes.
    text: Callable
    vector: Callable
    scope: Callable
    same_text: Callable
    # A memory's `updated_at` is the developer's (the API returns it, and
    # PATCH checks it for a stale write): a vector computed in the background
    # is not an edit. This drain named the column in every update to keep it;
    # since 2026-09-28 the column has no `onupdate` at all and only an edit
    # sets it (models.AgentMemory), so nothing here has to remember.


def _memory_vector(text):
    return _memory_engine.memory_vector(text)


def _fact_vector(text):
    return _facts.fact_vector(text)


def _fact_text(r) -> str:
    return _facts.fact_text(r.subject_canonical, r.subject_label, r.predicate_raw,
                            r.object_canonical, r.object_label)


_MEMORIES = _Kind(
    name="memory",
    model=AgentMemory,
    columns=(AgentMemory.id, AgentMemory.tenant_id, AgentMemory.agent_id,
             AgentMemory.content, AgentMemory.embedding_attempts),
    live=(AgentMemory.deleted_at.is_(None),),
    # Whole: memory_vector takes what a vector is computed from (VECTOR_CHARS).
    text=lambda db, r: r.content or "",
    vector=_memory_vector,
    scope=lambda r: dict(agent_id=r.agent_id, memory_id=r.id),
    same_text=lambda r: (AgentMemory.content == r.content, AgentMemory.deleted_at.is_(None)),
)

_FACTS = _Kind(
    name="fact",
    model=AgentFact,
    columns=(AgentFact.id, AgentFact.tenant_id, AgentFact.agent_id, AgentFact.source_memory_id,
             AgentFact.subject_canonical, AgentFact.subject_label, AgentFact.predicate_raw,
             AgentFact.object_canonical, AgentFact.object_label, AgentFact.embedding_attempts,
             AgentFact.judged_before_vector),
    live=(),
    text=lambda db, r: _fact_text(r),
    vector=_fact_vector,
    scope=lambda r: dict(agent_id=r.agent_id, memory_id=r.source_memory_id),
    same_text=lambda r: (),
)


def _set(db: Session, kind: _Kind, row_id, conditions: tuple, values: dict) -> int:
    M = kind.model
    return int(db.query(M).filter(M.id == row_id, *conditions)
               .update(values, synchronize_session=False) or 0)


def _due(kind: _Kind, now) -> tuple:
    M = kind.model
    return (M.embedding_status == PENDING, M.embedding.is_(None),
            or_(M.embedding_next_at.is_(None), M.embedding_next_at <= now), *kind.live)


def _each_account_its_share(db: Session, kind: _Kind, limit: int) -> list:
    """The accounts with a row due, the one whose oldest row waits longest
    first, at most `limit` of them: the claims of a pass go to them in turns
    (`_claims`), so each account is served the same number of times whatever
    its queue (fair queueing per account, Demers, Keshav and Shenker, SIGCOMM
    1989, as the facts drain, the batch worker and the webhook round).

    One install-wide queue by the row's own age (until the audit of 27 Oct
    2026) let an
    account that had many old rows, or put its old rows back at the head by
    declaring a name (`vector_state.cleared` keeps `created_at`), hold every
    other account's newer vectors, and the late judgement they make
    possible, behind all of them."""
    M = kind.model
    rows = (db.query(M.tenant_id).filter(*_due(kind, utc_now())).group_by(M.tenant_id)
            .order_by(func.min(M.created_at).asc(), M.tenant_id.asc()).limit(max(1, int(limit))).all())
    db.commit()
    return [t for (t,) in rows]


def _claim_one(db: Session, kind: _Kind, tenant_id):
    """The account's next row that is due, claimed: its attempt counted and
    its next wait scheduled, committed before any call is made. None when
    none is due. One at a time, so an attempt is spent only on a call that
    is made."""
    M = kind.model
    now = utc_now()
    r = (
        db.query(*kind.columns)
        .filter(M.tenant_id == tenant_id, *_due(kind, now))
        .order_by(M.created_at.asc())
        .limit(1)
        # Two replicas must not both call for one row: the second skips what
        # the first holds, and the claim keeps it away once released.
        .with_for_update(skip_locked=True, of=M)
        .first()
    )
    if r is None:
        db.commit()
        return None
    attempt = int(r.embedding_attempts or 0) + 1
    _set(db, kind, r.id, (), {"embedding_attempts": attempt,
                              "embedding_next_at": now + pause_after(attempt)})
    db.commit()
    return r, attempt


def _claims(db: Session, kind: _Kind, limit: int):
    """Up to `limit` claimed rows, one account's after another's in turns; an
    account whose queue ran dry leaves the rotation and its turns go to the
    others (`_each_account_its_share`)."""
    turns = deque(_each_account_its_share(db, kind, limit))
    taken = 0
    while turns and taken < limit:
        tenant_id = turns.popleft()
        claimed = _claim_one(db, kind, tenant_id)
        if claimed is None:
            continue
        taken += 1
        turns.append(tenant_id)
        yield claimed


def _drain(db: Session, kind: _Kind, limit: int, out: dict) -> None:
    M = kind.model
    failures_in_a_row = 0
    for r, attempt in _claims(db, kind, limit):
        out["claimed"] += 1
        emb = None
        try:
            # A memory's key reads its facts: the read ends before the call,
            # so no transaction is held while the model is asked.
            text = kind.text(db, r)
            db.commit()
            with llm_usage.attributed(r.tenant_id, **kind.scope(r)):
                emb = kind.vector(text)
        except Exception as e:  # noqa: BLE001 - a failed call is an attempt, never a crash
            db.rollback()
            logger.warning("[vectors] %s %s: attempt %d/%d gave no vector: %s",
                           kind.name, r.id, attempt, ATTEMPTS, log_safe.error(e))
        as_claimed = (M.embedding_status == PENDING, M.embedding_attempts == attempt,
                      *kind.same_text(r))
        closings: dict = {}
        embedder_failed = not emb
        if emb and kind is _FACTS and not r.judged_before_vector:
            # The embedder answered: whatever the judgement does, this row is
            # no sign that the embedder is down (`failures_in_a_row`).
            failures_in_a_row = 0
            try:
                emb, closings = _judged_with_its_vector(db, r, attempt, emb)
            except Exception as e:  # noqa: BLE001 - one row never ends the pass
                # Anything the judgement raised that it does not handle is this
                # row's failed attempt, on the ladder and under the ATTEMPTS
                # ceiling, never the end of the pass for every account behind
                # it (audit of 18 Oct 2026).
                db.rollback()
                logger.error("[vectors] fact %s: its late judgement failed, the vector waits "
                             "for it: %s", r.id, log_safe.stack(e))
                emb, closings = None, {}
        if emb:
            failures_in_a_row = 0
            n = _set(db, kind, r.id, as_claimed, {
                "embedding": emb, "embedding_status": DONE, "embedding_next_at": None,
                **({"judged_before_vector": False} if kind is _FACTS else {})})
            out["processed"] += n
            if not n:
                # Written or erased elsewhere since the claim: that path owns
                # the fact, and the closings its vector allowed go with it.
                db.rollback()
                closings = {}
        else:
            # A judgement with no answer is the writer's, not the embedder's:
            # a region whose writer is down must not stop the pass for the
            # vectors of the accounts of the other regions.
            failures_in_a_row += int(embedder_failed)
            if attempt >= ATTEMPTS:
                n = _set(db, kind, r.id, as_claimed, {
                    "embedding_status": ERROR, "embedding_next_at": None})
                out["gave_up"] += n
                if n:
                    logger.warning("[vectors] %s %s: no vector after %d attempts, left "
                                   "'error' (`manage reextract` queues it again)",
                                   kind.name, r.id, attempt)
            else:
                out["failed"] += 1
        if closings:
            # In the transaction that writes the closings, so the two commit
            # together (audit of 17 Oct 2026: enqueued after the commit, a
            # failure in between kept the closing and lost its event).
            _facts.emit_invalidations(db, r.tenant_id, [
                {"id": fact_public_id(closing_id), "invalidated": [fact_public_id(i) for i in ids]}
                for closing_id, ids in closings.items()])
        db.commit()
        if failures_in_a_row >= FAILURES_BEFORE_STOPPING:
            logger.warning("[vectors] %d %s vectors failed in a row: the provider looks "
                           "down, the rest wait for the next pass", failures_in_a_row, kind.name)
            return


def _judged_with_its_vector(db: Session, r, attempt: int, emb) -> tuple:
    """A fact's late vector, and the judgements it makes possible
    (facts.contradiction.judge_after_the_vector: the semantic stage its write
    could not run), in the transaction that stores it. Returns (the vector to
    store, or None when the judge gave no answer and the claim is retried
    like a failed call; {closing fact id: [closed ids]}). A fact closed by
    somebody else while the judge was asked keeps its vector and makes no
    closing (`_ended_while_judged`)."""
    fact = db.query(AgentFact).filter(AgentFact.id == r.id, AgentFact.embedding_status == PENDING,
                                      AgentFact.embedding_attempts == attempt).first()
    if fact is None:
        return emb, {}  # changed since the claim: the compare-and-set drops the vector
    # Its end as stored before the judge is asked, read from the row and not
    # from the session's copy, which can be older (expire_on_commit=False).
    ended = db.query(AgentFact.invalid_at).filter(AgentFact.id == r.id).scalar()
    try:
        # The judge and the vocabulary's votes go to the writer of the fact's
        # project region, as on its write: never the install's default writer
        # for a project in another region (audit of 17 Oct 2026). A region
        # with no writer gives no judgement, and nothing leaves.
        with llm_usage.attributed(r.tenant_id, **_FACTS.scope(r)), \
                regions.writing_for(db, r.tenant_id, fact.project_id):
            closings = contradiction.judge_after_the_vector(db, r.tenant_id, fact, emb)
    except (contradiction.JudgeUnavailable, ResolutionUnavailable, CandidatesUnread) as e:
        # The same failures as on the write (facts.service.MODEL_FAILURES):
        # the judge with no verdict, the vocabulary's vote with no answer
        # (memory_core's lazy single-value vote, ResolutionUnavailable), a
        # stage of the candidates that could not be read. Escaped, the last
        # two ended the pass for every account and skipped the ATTEMPTS
        # ceiling, so the row came back every six hours for ever (audit of
        # 18 Oct 2026).
        db.rollback()
        logger.warning("[vectors] fact %s: the judgement its vector allows had no answer, "
                       "the vector waits for it: %s", r.id, log_safe.error(e))
        return None, {}
    except IntegrityError:
        # Erased for good while the judge was asked (DELETE /v1/agents/{id},
        # an end user's erasure): closing the fact it contradicts points at a
        # row that is gone, and the flush refuses it. Raised, it ended the
        # whole pass, and the other vectors waited for the next one (audit of
        # 17 Oct 2026). The old fact stays open, rightly; the vector's
        # compare-and-set finds no row and writes nothing.
        db.rollback()
        logger.info("[vectors] fact %s was erased while its late judgement ran; "
                    "its closings are dropped", r.id)
        return emb, {}
    changed = _ended_while_judged(db, r.id, ended, _closers(closings, r.id)) if closings else False
    if changed is None:
        db.rollback()
        return None, {}  # not checked: retried like a failed call, never committed unchecked
    if changed:
        # Forgotten (or its memory deleted) while the judge was asked: the
        # forget answered "forgotten", and a moment later the forgotten fact
        # closed the true one it contradicted, which nothing reopened (audit
        # of 17 Oct 2026). The same for a later fact that closes this one
        # (audit of 18 Oct 2026). Its judgement goes; its vector is still
        # its own.
        db.rollback()
        logger.info("[vectors] fact %s, or a fact its late judgement closes with, was closed "
                    "while the judgement ran; its closings are dropped", r.id)
        return emb, {}
    return emb, closings


def _closers(closings: dict, fact_id) -> list:
    """The facts other than `fact_id` that close something in `closings`:
    the later facts that close the late one, and an older candidate that
    began after it."""
    return [c for c in closings if c != fact_id]


def _ended_while_judged(db: Session, fact_id, ended, closers=()) -> Optional[bool]:
    """Whether another transaction closed, moved the end of, or removed the
    fact since its end was read as `ended`, or ended or removed one of the
    `closers` (the other facts its judgement closes with); None when it could
    not be told.

    The rows are locked first, in the order of their ids so two drains never
    wait on each other, and nothing can change them between this check and
    the commit of the judgement: a forget that comes later waits, and closes
    after. The committed state is then read in a session of its own, because
    this transaction may have closed some of them itself and reads its own
    change. A closer must still be true now (`temporal.true_at`, what the
    engine's selection asked of it before the judge was called: memory_core
    selects the later facts inside `after_the_vector`, so their end before the
    call is not seen here). Until 18 Oct 2026 only the late fact was checked:
    a later fact forgotten while the judge was asked still closed the true
    one, with a fact.invalidated naming the forgotten fact (audit of 18 Oct
    2026)."""
    others = {c for c in closers if c != fact_id}
    try:
        (db.query(AgentFact.id).filter(AgentFact.id.in_([fact_id, *others]))
         .order_by(AgentFact.id).with_for_update().all())
        from ..db import get_own_sessionmaker

        # Another connection while this one holds its locks: an own session,
        # never a second one of the pool this one came from (audit of 27 Oct
        # 2026).
        with get_own_sessionmaker()() as other:
            row = other.query(AgentFact.invalid_at).filter(AgentFact.id == fact_id).first()
            still = ({i for (i,) in other.query(AgentFact.id)
                      .filter(AgentFact.id.in_(others), true_at())} if others else set())
        return row is None or row[0] != ended or still != others
    except Exception as e:  # noqa: BLE001 - a lock that failed is no proof it is open
        logger.warning("[vectors] fact %s: could not check it is still open after its late "
                       "judgement, both wait for the next attempt: %s", fact_id,
                       log_safe.error(e))
        return None


def process_pending_vectors(db: Session, *, limit: int = 50) -> dict:
    """One pass of the vector drain: up to `limit` memories and `limit` facts
    that are due. Returns counts; nothing to do (or no embedder) is {}."""
    from ..core import llm as _llm

    if not _llm.embedding_configured():
        return {}
    out = {"claimed": 0, "processed": 0, "failed": 0, "gave_up": 0}
    for kind in (_MEMORIES, _FACTS):
        _drain(db, kind, max(1, int(limit)), out)
    # The relations with no centroid (18 Oct 2026): born so when their first
    # phrase could not be embedded, and every relation of every end user
    # after migration e7d1b3f9a2c6 (one vocabulary per end user of a project).
    from .predicates import centroids_missing, fill_missing_centroids

    centroids = fill_missing_centroids(db)
    if centroids:
        out["centroids"] = centroids
        # A backlog of them (every relation, after e7d1b3f9a2c6): the worker
        # starts the next pass at once rather than VECTORS_EVERY seconds from
        # now, as for the facts drain's `more` (worker._loop). Only a pass
        # that gave some their centroid says it: one that failed on its first
        # relation gave none, so a failing embedder is asked at most once
        # more before the worker rests.
        if centroids_missing(db):
            out["more"] = True
    return out if out["claimed"] or centroids else {}


# ── another embedding model: `manage recompute-vectors` ─────────────────────
#
# Two models' vectors are not comparable. After EMBEDDING_PROVIDER (or
# EMBEDDING_MODEL) changes, every stored vector is in the other model's space:
# a question finds those rows badly, and a new fact is judged against them
# badly. `recompute` gives each row the vector of its text on the model in
# force, computed as its writer computes it (`memory_vector`, `fact_vector`),
# after the relations' centroids (predicates.recompute_centroids): the order
# set for both engines on 2026-09-29, the same as the hosted product's.
#
#     embedding_status  stale   a vector from the model before. It stays until
#                               the new one is written: a failed call leaves
#                               it, and the row waits for the next run.
#
# Resumable. A run that finds no stale row marks every `done` vector stale and
# starts; a run that finds some was interrupted, and goes on with them. The
# write is a compare-and-set on `stale` (and, for a memory, on its text): a row
# edited meanwhile already has the new model's vector and keeps it. The drain
# above does not see stale rows (they have a vector), so the two never compute
# the same one.


def _live(model) -> tuple:
    return (model.deleted_at.is_(None),) if model is AgentMemory else ()


def to_recompute(db: Session) -> dict:
    """What a run would do. `resuming` when an earlier run left stale rows."""
    stale = {name: db.query(M).filter(M.embedding_status == STALE, *_live(M)).count()
             for name, M in (("facts", AgentFact), ("memories", AgentMemory))}
    if any(stale.values()):
        return {"resuming": True, **stale}
    with_a_vector = {
        name: db.query(M).filter(M.embedding.isnot(None), M.embedding_status == DONE,
                                 *_live(M)).count()
        for name, M in (("facts", AgentFact), ("memories", AgentMemory))}
    return {"resuming": False, **with_a_vector}


def mark_stale(db: Session) -> dict:
    """The start of a run: every current vector is from the model before.
    Commits."""
    out = {}
    for name, M in (("facts", AgentFact), ("memories", AgentMemory)):
        out[name] = int(db.query(M).filter(M.embedding.isnot(None), M.embedding_status == DONE,
                                           *_live(M))
                        .update({"embedding_status": STALE}, synchronize_session=False) or 0)
    db.commit()
    return out


def _recompute(db: Session, kind: _Kind, limit: int, progress) -> dict:
    """One kind's stale rows, by id. A failed call leaves the row stale;
    FAILURES_BEFORE_STOPPING in a row stop the run (`stopped`)."""
    M = kind.model
    out = {"recomputed": 0, "failed": 0, "stopped": False}
    last = None
    in_a_row = 0
    while True:
        q = db.query(*kind.columns).filter(M.embedding_status == STALE, *kind.live)
        if last is not None:
            q = q.filter(M.id > last)
        batch = q.order_by(M.id).limit(200).all()
        db.commit()
        if not batch:
            return out
        for r in batch:
            last = r.id
            emb = None
            try:
                text = kind.text(db, r)
                db.commit()
                with llm_usage.attributed(r.tenant_id, **kind.scope(r)):
                    emb = kind.vector(text)
            except Exception as e:  # noqa: BLE001 - a failed call keeps the old vector
                db.rollback()
                logger.warning("[vectors] %s %s: not recomputed, keeps its old vector: %s",
                               kind.name, r.id, log_safe.error(e))
            if emb:
                in_a_row = 0
                out["recomputed"] += _set(db, kind, r.id, (M.embedding_status == STALE,
                                                           *kind.same_text(r)),
                                          {"embedding": emb, "embedding_status": DONE,
                                           "embedding_next_at": None})
                db.commit()
            else:
                out["failed"] += 1
                in_a_row += 1
                if in_a_row >= FAILURES_BEFORE_STOPPING:
                    out["stopped"] = True
                    return out
            done = out["recomputed"] + out["failed"]
            if progress and done % 500 == 0:
                progress(kind.name, done)
            if limit and done >= limit:
                return out


def recompute(db: Session, *, limit: int = 0, progress=None) -> dict:
    """The facts', then the memories' stale vectors, on the model in force.
    `limit` rows of each at most (0: all). Call `mark_stale` first on a new
    run, and recompute the centroids before this."""
    out = {}
    for key, kind in (("facts", _FACTS), ("memories", _MEMORIES)):
        out[key] = _recompute(db, kind, int(limit or 0), progress)
        if out[key]["stopped"]:
            break
    return out


# ── the start's check: are the stored vectors this model's? ─────────────────
#
# The default embedder changed in 0.1.19, from Gemini to EmbeddingGemma on this
# machine, and both give 768 numbers. An install upgraded with no
# EMBEDDING_PROVIDER in .env would have started, put every question in the new
# space and compared it with every memory in the old one. Nothing fails there:
# searches return the wrong memories, and the judge compares a new fact with
# the wrong old ones. The same happens to anybody who changes the setting and
# forgets `recompute-vectors`.
#
# So the install remembers the model of its vectors (db.models.InstallState,
# `embedding_model`), and the API and the worker compare it with the model in
# force before they start: a difference stops them, with what to do, unless a
# `recompute-vectors` run is moving the vectors (it writes the new name when
# it ends). An install from before the record has none: the first start
# computes the latest rows' vectors again on the model in force and compares
# them with the stored ones, once. One model gives one text its vector again
# (cosine 1.00 on all six rows of three bench databases, 2026-10-06), two
# models give unrelated ones; how unrelated Gemini and EmbeddingGemma are
# nobody here has measured (EmbeddingGemma was distilled from Gemini's
# embeddings), so the line is drawn near the first: under SAME on every row
# is another model. A failed call or no embedder decides nothing, the start
# goes on and the next one tries again: a check that cannot run must not keep
# a working install down.
EMBEDDING_MODEL = "embedding_model"
CHECKED = 3   # of each kind, the latest with a vector: facts first, their text is short
SAME = 0.95


class VectorsOfAnotherModel(RuntimeError):
    """The stored vectors were not computed by the model in force."""


def _cosine(a, b) -> float:
    """Cosine similarity, from the engine's one cosine (`memory_core.vector_search.cosine`,
    a distance; the code audit of 17 Oct 2026, R2-DUP-01: there were two, with
    different contracts). 0.0 when it cannot be computed (a zero or unusable
    vector), as before: such a vector is not the model's."""
    distance = cosine([float(x) for x in a], [float(x) for x in b])
    return 0.0 if distance is None else 1.0 - distance


def recorded_model(db: Session) -> Optional[str]:
    """The model the stored vectors were computed with, as recorded."""
    from ..db.models import InstallState

    row = db.get(InstallState, EMBEDDING_MODEL)
    return row.value if row is not None else None


def record_model(db: Session, name: str) -> None:
    """Commits. One statement, so two processes starting on a new database
    both record it: a read then an insert let the second die on the first
    one's row (30 Oct 2026)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from ..db.models import InstallState

    db.execute(pg_insert(InstallState).values(name=EMBEDDING_MODEL, value=name)
               .on_conflict_do_update(index_elements=[InstallState.__table__.c.name],
                                      set_={"value": name, "updated_at": func.now()}))
    db.commit()


def compare_with_stored(db: Session) -> dict:
    """{"verdict": "same" | "other" | "nothing", "cosines": [...], "why": ...}:
    the latest rows' vectors computed again on the model in force. "nothing"
    when there is no row with a vector, no embedder, or no vector came back
    (`why` says which)."""
    from ..core import llm as _llm

    if not _llm.embedding_configured():
        return {"verdict": "nothing", "cosines": [], "why": "no embedder is configured"}
    cosines = []
    for kind in (_FACTS, _MEMORIES):
        M = kind.model
        rows = (db.query(*kind.columns, M.embedding)
                  .filter(M.embedding.isnot(None), M.embedding_status == DONE, *kind.live)
                  .order_by(M.created_at.desc()).limit(CHECKED).all())
        for r in rows:
            try:
                text = kind.text(db, r)
                with llm_usage.attributed(r.tenant_id, **kind.scope(r)):
                    fresh = kind.vector(text)
            except Exception as e:  # noqa: BLE001 - a failed call decides nothing
                return {"verdict": "nothing", "cosines": cosines, "why": f"no vector: {e}"}
            if not fresh:
                return {"verdict": "nothing", "cosines": cosines, "why": "no vector came back"}
            cosines.append(round(_cosine(r.embedding, fresh), 3))
    if not cosines:
        return {"verdict": "nothing", "cosines": [], "why": "no stored vector yet"}
    return {"verdict": "same" if max(cosines) >= SAME else "other", "cosines": cosines,
            "why": None}


def _setting_for(model: str) -> str:
    from ..core.embedding_model import NAME as LOCAL

    return "EMBEDDING_PROVIDER=local" if model == LOCAL else \
        f"EMBEDDING_PROVIDER=gemini (EMBEDDING_MODEL={model})"


def _move_them(model: str) -> str:
    return (f"To move them to {model}: `docker compose run --rm api python -m "
            f"korely_agent.manage recompute-vectors --apply`, then start again; the "
            f"service also starts while a run is under way.")


def check_the_vectors(db: Session) -> dict:
    """The start's decision (above), without raising: {"start": bool,
    "level": "info" | "warning", "say": ...}. Records the model when the
    install had no record and its vectors are found to be the model's."""
    from ..config import get_settings
    from ..core.llm import embedding_model_name

    model, provider = embedding_model_name(), get_settings().embedding_provider
    harm = ("Two models' vectors cannot be compared: searches would return the wrong "
            "memories and new facts would be judged against the wrong ones, so the "
            "service does not start.")
    recorded = recorded_model(db)
    has_vectors = any(db.query(M.id).filter(M.embedding.isnot(None)).first() is not None
                      for M in (AgentFact, AgentMemory))
    if not has_vectors:
        # Nothing to protect: an install that changes its embedder before its
        # first vector starts on the new one.
        if recorded != model:
            record_model(db, model)
        return {"start": True, "level": "info", "say": f"no stored vector yet: {model} recorded"}
    if recorded == model:
        return {"start": True, "level": "info", "say": f"the stored vectors are {model}'s"}
    if recorded is not None:
        left = to_recompute(db)
        if left["resuming"]:
            return {"start": True, "level": "warning",
                    "say": f"a recompute-vectors run is moving the vectors from {recorded} to "
                           f"{model}: {left['facts']} facts and {left['memories']} memories "
                           f"left, found badly until it ends; run it again to finish"}
        return {"start": False, "level": "error",
                "say": f"The vectors stored here were computed by {recorded}, and this "
                       f"install computes them with {model} now (EMBEDDING_PROVIDER="
                       f"{provider}). {harm} To keep them, set {_setting_for(recorded)} "
                       f"in .env and start again. {_move_them(model)}"}
    out = compare_with_stored(db)
    cos = ", ".join(f"{c:.2f}" for c in out["cosines"])
    if out["verdict"] == "same":
        record_model(db, model)
        return {"start": True, "level": "info",
                "say": f"the stored vectors are {model}'s (cosine {cos}): recorded"}
    if out["verdict"] == "nothing":
        return {"start": True, "level": "warning",
                "say": f"the stored vectors were not compared with {model} ({out['why']}); "
                       f"the next start tries again"}
    upgraded = (" Upgraded from 0.1.18 or earlier with no EMBEDDING_PROVIDER in .env? Then "
                "they are Gemini's, the default before 0.1.19: set EMBEDDING_PROVIDER=gemini "
                "in .env and run `docker compose up -d --build`." if provider == "local" else "")
    return {"start": False, "level": "error",
            "say": f"The vectors stored here were computed by another model than {model} "
                   f"(EMBEDDING_PROVIDER={provider}): computed again on it, the latest "
                   f"{len(out['cosines'])} match none of their stored vectors (cosine {cos}; "
                   f"one model gives 1.00). {harm}{upgraded} To keep them, set "
                   f"EMBEDDING_PROVIDER back to the one that computed them. "
                   f"{_move_them(model)}"}


def refuse_vectors_of_another_model() -> None:
    """The start's check, in its own session. Raises `VectorsOfAnotherModel`
    with what to do; anything else that goes wrong is logged and the start
    goes on."""
    try:
        from ..db import get_sessionmaker

        db = get_sessionmaker()()
        try:
            out = check_the_vectors(db)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 - see the docstring
        logger.warning("[embeddings] could not check the stored vectors' model; starting "
                       "anyway: %s", log_safe.stack(e))
        return
    if not out["start"]:
        raise VectorsOfAnotherModel(out["say"])
    (logger.warning if out["level"] == "warning" else logger.info)("[embeddings] %s", out["say"])
