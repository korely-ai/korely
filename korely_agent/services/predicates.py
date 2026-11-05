"""Relation canonicalization.

2026-09-27: the rules below (the three questions, how their answers are read,
when a failure refuses the write) now live in `services/memory_core/`, the
facts engine (shared byte-for-byte with an earlier product until 11 Oct 2026;
since then it lives only here). This module keeps the self-hosted tables (one
vocabulary per end user of a project, `end_user_vocabulary`) and the
history of the measurements that chose every line, which is why the long
account below stays here.

The extractor writes open relation phrases, which is what makes the store useful
on a domain it has never seen: an insurer's `covered_under` and `claim_status`
are not in anybody's fixed vocabulary. The cost is that the same relation comes
back phrased differently every time. Over one unchanged sentence we observed
`has_plan`, `uses_plan`, `subscribes_to`, `plan`, `uses` and `moved_to`, and
contradiction detection matches candidates on the canonical predicate, so a plan
change written twice was stored as two facts that never met.

This is a named problem: canonicalizing an Open KB (Galarraga et al., CIKM 2014;
CESI, WWW 2018). Both cluster relation phrases rather than fixing a vocabulary,
which is the right shape here.

**Embedding distance alone does not work, measured.** On bare phrases the ranges
invert: `uses_plan`/`has_plan` sit at 0.347 and must merge, while
`lives_in`/`works_at` sit at 0.293 and must not. Adding the argument as side
information helps and still overlaps: 0.382 for `has_plan`/`subscribes_to`
against 0.329 for `lives_in`/`works_at`. No threshold separates them, because
embeddings measure topical similarity and we are asking about relational
identity: "lives in Milan" and "works at Apple" really are alike, and "has plan"
and "subscribes to" really are not.

So the structure of CESI is kept and the decider is swapped: embeddings narrow
the field to a handful of candidates, and the model answers whether the new
phrase names one of them. Asked once per unseen phrase, then cached forever.

**How well that works, measured.** Ten tenants, the same sixteen phrases, a
different arrival order each time, both arms of one experiment. The only
difference between the arms is the last line of the prompt below, which tells
the model to prefer null when unsure.

                                    shipped     without that line
    plan phrases collapsed to       5 (median)  3 (median)
                                    baseline without a model: 5
    subscribes_to merged            2 / 10      8 / 10
    runs with a FALSE merge         0 / 10      1 / 10

Read both columns or neither. The shipped arm never destroys anything: zero
false merges across likes/dislikes, manages/reports_to, owns/rents and
lives_in/born_in, which are the pairs where an over-eager merge is unrecoverable
rather than merely untidy. What it also does is barely merge: the median run
equals what the surface normalizer delivers alone and for free, and
`subscribes_to`, one of the exact phrasings from the production failure this
exists to fix, joins its cluster twice in ten runs.

Dropping the line fixes most of that - `subscribes_to` merges eight times in ten
- and once in ten runs it put `reports_to` into `manages`. Those are inverse
relations, and merging them erases the direction of the relationship for good:
"Anna manages the team" and "Anna reports to the team" become one fact.

**Turning the model's thinking off makes this better, measured.** The two calls
below ask for a yes/no and a number, and they were being answered by a model
that reasons first: 1608 thinking tokens to say `{"same_as": 1}`, 8.2 seconds.
Asked not to think, the same call takes 0.57s. Re-running the ten runs above
with thinking off, same prompt, same hard controls:

                                    thinking on   thinking off
    plan phrases collapsed to       5 (median)    3 (median)
    subscribes_to merged            2 / 10        5 / 10
    runs with a FALSE merge         0 / 10        0 / 10

Better on merging, unchanged on the thing that must not move, and fourteen times
faster on the call.

**But only on that call, and finding out cost a wrong shipped afternoon.** The
measurement above covers merging and separation. `_classify_functional` answers
a third question, whether a relation holds one value at a time, and that axis
was not measured before the switch was flipped for both calls. It degrades:

    eighteen relations with known answers, thinking on    18 / 18
                                           thinking off   16 / 18

The two it loses are `on_plan` and `signed_up_for`, which is to say the plan
relations the flagship example turns on. Classified multi-valued, the
deterministic supersede never fires and the store cheerfully holds that one
customer is on three plans at once. It was caught by the Markdown export, which
had existed for about ten minutes: the file said a customer lived in two cities,
which is a thing a table shows you and an API response does not. The likely reason is the last line of the prompt: "prefer
null when unsure" is exactly the hedge a chain of thought amplifies, and given
room to reason the model talks itself into "these might be different". Without
it, it answers on immediate semantic similarity, which for relation phrases is
the better instinct.

Ten runs on one corpus with one model is not a law, and zero false merges in ten
runs bounds that rate loosely rather than proving it zero. It is enough to
choose a configuration that is better or equal on every axis measured and much
cheaper, and it is written here so the next person does not re-derive it.

An earlier version of this measurement used four easy controls (lives_in,
works_at, likes, born_in) and reported zero false merges in BOTH arms, which
read as "the cautious line costs merges and buys nothing". That reading was an
artifact of controls nobody would confuse. The hard pairs above were added for
exactly that reason, and they reversed the conclusion.

So the line stays, and what it buys is stated rather than assumed: a floor. The
store converges to never-worse-than-the-normalizer, sometimes better, never
wrong. Neither prompt is the fix; one yes/no question per phrase is not enough
mechanism for this, and that is a design change rather than a knob.

`_classify_functional` is a separate question and is load-bearing - the
deterministic supersede reads `is_functional` from the cluster - so this is not
a case for removing the machinery, only for not claiming more than it does.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Optional, Tuple
from uuid import UUID

from sqlalchemy import and_, delete, exists, update
from sqlalchemy.orm import Session, aliased

from ..db import PredicateAlias, PredicateCluster
from .memory_core import store as _store
from .memory_core import model_json as _json_reader
from .memory_core import vocabulary as _vocabulary
from .memory_core import log_safe

logger = logging.getLogger(__name__)

# The distance cut of the shortlist is the embedding model's since memory_core
# 2.4.0 (thresholds.current().shortlist), read where the store filters: a copy
# taken here at import kept Gemini's 0.65 whatever model made the vectors, so
# there is none here (the unused `_SHORTLIST` name went in the audit of 27 Oct
# 2026).


# memory_core 3.3.0 `lazy_votes` (the hosted product's
# AGENTS_LAZY_SINGLE_VALUE before it ran this code): a new relation is born undecided and its three
# single-value votes are asked the first time a judge meets two facts of it
# (`facts.contradiction._ensure`), not at its birth. Half of LoCoMo's
# relations are used by one fact: their votes are three calls nobody reads.
# The vote is typed by the two values the judge has in conflict, not the birth
# example (8 Oct: `reads` born with "reading" voted one value and closed every
# book). On since 0.1.23 (8 Oct 2026): measured with the
# 3.3.0 pack and confirmations (LoCoMo, 7-8 Oct 2026, gpt-oss): with the lazy
# votes, the prefix and the event day, list-free, 610 answers of 762 against
# 618 of the same-day reading (inside noise), facts closed by mistake 53 -> 17,
# model calls per memory 2.84 -> 2.32.
LAZY_SINGLE_VALUE = True


def end_user_vocabulary(tenant_id: UUID, project_id: Optional[UUID],
                            end_user_id: Optional[str]) -> _store.Memory:
    """One vocabulary per end user of a project (decided 18 Oct 2026:
    every end user is a thing of its own, every project is a thing of its
    own). A relation carries a value of the facts it was learned from (its
    example and the centroid made of it) and decisions taken on them (one
    value or many, the family, its name): one per tenant until 18 Oct, they
    crossed projects and regions (the code audit of 17 Oct 2026); one per
    project for a day, they crossed end users (audit of 18 Oct 2026). Now an
    end user's vocabulary is learned from that end user's facts alone.
    `project_id` None is the vocabulary of the facts of no project,
    `end_user_id` None that of the facts of no end user."""
    return _store.Memory(
        facts=None,
        alias=None,
        relations=and_(PredicateCluster.tenant_id == tenant_id,
                       PredicateCluster.project_id.is_not_distinct_from(project_id),
                       PredicateCluster.end_user_id.is_not_distinct_from(end_user_id)),
        variants=and_(PredicateAlias.tenant_id == tenant_id,
                      PredicateAlias.project_id.is_not_distinct_from(project_id),
                      PredicateAlias.end_user_id.is_not_distinct_from(end_user_id)),
        new_relation=dict(tenant_id=tenant_id, project_id=project_id, end_user_id=end_user_id),
    )


_TABLES = _store.Tables(
    Fact=None, Alias=None, source="", sources="", alias_fact="",
    Relation=PredicateCluster, Variant=PredicateAlias,
)


def merge_vocabulary(db: Session, *, source: tuple, target: tuple, move_the_rest: bool) -> int:
    """Join the vocabularies of one scope, (tenant_id, project_id), to those of
    another: each end user's to the same end user's, never another end user's
    (`end_user_vocabulary`). A phrase the end user already resolves in the
    target goes; a relation the end user already has there keeps the target's
    row, and the source's phrases for it join it; any other relation moves
    whole with `move_the_rest`, its phrases with it, and stays where it is
    without (the caller moves it). The one merge of an account claim
    (hosted/claim) and of a project's deletion (services/projects): the scope
    of the vocabulary changed twice on 18 Oct 2026, and each time both copies
    were edited by hand (audit of 19 Oct 2026). Returns how many relations moved
    whole. Does not commit.

    Three statements, five with `move_the_rest`, each decided in the
    database, and no row read into the process (audit of 20 Oct 2026). It
    read every relation of the target, centroid included, and every phrase
    there, of every end user, on every
    project deletion, the deletion of an empty project too; then every
    relation of the source whole. A target row is now only ever met as the
    twin of a source row: the same end user (NULL with NULL, as the unique
    indexes are NULLS NOT DISTINCT) and the same canonical name, or the same
    phrase. The order is the old loop's: the phrases the target resolves go
    first, so the phrases that join a twin or move cannot meet one there."""
    C, A = PredicateCluster, PredicateAlias
    TC, TA = aliased(PredicateCluster), aliased(PredicateAlias)
    to_tenant, to_project = target

    def of(scope, M):
        tenant_id, project_id = scope
        return and_(M.tenant_id == tenant_id, M.project_id.is_not_distinct_from(project_id))

    # The source relation's twin: the target's relation of the same end user and name.
    twin = and_(of(target, TC), TC.end_user_id.is_not_distinct_from(C.end_user_id),
                TC.canonical == C.canonical)
    quietly = {"synchronize_session": False}
    where = {A.tenant_id: to_tenant, A.project_id: to_project}

    db.flush()
    db.execute(delete(A).where(of(source, A), exists().where(
        of(target, TA), TA.end_user_id.is_not_distinct_from(A.end_user_id),
        TA.surface == A.surface)).execution_options(**quietly))
    db.execute(update(A).where(A.cluster_id == C.id, of(source, C), twin)
                 .values({**where, A.cluster_id: TC.id}).execution_options(**quietly))
    db.execute(delete(C).where(of(source, C), exists().where(twin)).execution_options(**quietly))
    moved = 0
    if move_the_rest:
        db.execute(update(A).where(A.cluster_id == C.id, of(source, C))
                     .values(where).execution_options(**quietly))
        moved = db.execute(update(C).where(of(source, C))
                             .values({C.tenant_id: to_tenant, C.project_id: to_project})
                             .execution_options(**quietly)).rowcount or 0
    db.flush()
    return moved


def _ask(stage: Optional[str] = None):
    """The model call in the shape memory_core expects: (prompt, think) ->
    text. Looked up at call time, so the tests' stub provider is the one used.
    `stage`: the cost row's stage, the vocabulary's unless given (an
    operator's run, `llm_usage.OPERATOR`)."""
    from ..core import llm as _llm

    client = _llm.get_provider()
    if client is None:
        return None

    from ..config import get_settings

    # VOCABULARY_MODEL, or the provider's default (EXTRACTION_MODEL) when unset.
    model = get_settings().vocabulary_model or None

    def ask(prompt: str, think: bool) -> str:
        from . import llm_usage

        with llm_usage.stage(stage or llm_usage.PREDICATES):
            r = client.complete(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                max_tokens=2048, temperature=0.0, json_only=True,
                # Thinking OFF for "same relation?" and "which family?", ON for
                # "one value at a time?": measured, see the module docstring.
                reasoning=think,
            )
        return _readable(r.text)
    return ask


def writers_ask():
    """`_ask` for the engine's other questions about a write's names, asked of
    the same model as the vocabulary's (memory_core 6.1.0, facts/objects.py:
    "is this new object the same thing as one the memory holds?"), its cost
    rows under their own stage (`llm_usage.OBJECTS`)."""
    from . import llm_usage

    return _ask(llm_usage.OBJECTS)


def _readable(text: Optional[str]) -> str:
    """The model's answer for the vocabulary's reader (memory_core
    `vocabulary._json`): the one object the answer holds, read with the
    engine's rule for a model's JSON (`model_json.read_object`,
    CONTRIBUTING.md), else the whole answer for that reader to try. Until the
    audit of 17 Oct 2026 it went through `core.llm.extract_json` first,
    which cuts from the first bracket of either kind: 'Answer [see note]:
    {"same_as": 2}' became '[see note]', no answer, and with `severe` a write
    refused for an answer that could be read."""
    import json

    found = _json_reader.read_object(text)
    if isinstance(found, dict):
        return json.dumps(found)
    return text or ""


def vocabulary_embedder():
    """The embedder in the shape memory_core expects, or None when there is
    none. Looked up at call time, like `_ask`.

    memory_core 1.2.0: on Postgres an `embed` that
    RAISES is a failed resolution (ResolutionUnavailable, the write is not
    kept and is retried), and one that is None is "no embedder" (the store
    works and does not merge). This adapter used to be a function calling
    `core.llm.embed`, which returns None when its call fails, so a failed call
    read as a missing vector: the phrase was filed as a new relation with no
    centroid, which no later phrase can ever join. Now no key is None, and a
    key with a call that fails raises (`core.llm.embed_or_raise`).
    """
    from ..core import llm as _llm

    if not _llm.embedding_configured():
        return None

    # A relation phrase is only ever compared with other phrases (and with the
    # centroids made of them), never looked for by a question: sentence
    # similarity on the local model, RETRIEVAL_DOCUMENT on Gemini as before
    # (core/llm.TASK_SIMILARITY).
    def embed(text: str):
        return _llm.embed_or_raise(text, task=_llm.TASK_SIMILARITY)
    return embed


def centroid_text(cluster) -> str:
    """The text a relation's centroid is the vector of, as memory_core writes
    it: a new relation's centroid is the vector of its first phrase with the
    object it was seen with (store, `text_for_embedding(surface,
    object_label)`), and its canonical name is that phrase. A relation learned
    before `example_object` was kept has its name alone."""
    return _vocabulary.text_for_embedding(cluster.canonical, cluster.example_object or "")


# What a centroid is computed from, read as plain values: an ORM row would be
# refreshed after each commit, and a relation an erasure deleted meanwhile
# would raise there instead of being skipped.
_WHAT_A_CENTROID_IS_MADE_OF = (PredicateCluster.id, PredicateCluster.tenant_id,
                               PredicateCluster.project_id, PredicateCluster.canonical,
                               PredicateCluster.example_object)


def fill_missing_centroids(db: Session, *, limit: int = 20) -> int:
    """The relations with no centroid get one, `limit` per pass of the vector
    drain (services/vectors.py), which starts the next pass at once while
    some are still missing (`centroids_missing`): the vector of the name with
    the relation's own example, a value of its end user's facts
    (`centroid_text`), computed in the project's region. A relation is born
    without one when its first phrase could not be embedded, and migration
    e7d1b3f9a2c6 (one vocabulary per end user of a project) leaves every
    relation of every end user without one: a vector made from another end
    user's value is not carried (the audit of 18 Oct 2026). Without a centroid
    a relation is not offered by similarity to a new phrase, so until it has
    one each new phrasing of it becomes a relation of its own (audit of 19
    Oct 2026).
    Stops at the first failure: the next pass tries again. Returns how many."""
    from . import llm_usage, regions

    rows = (db.query(*_WHAT_A_CENTROID_IS_MADE_OF).filter(PredicateCluster.centroid.is_(None))
            .order_by(PredicateCluster.created_at, PredicateCluster.id).limit(limit).all())
    if not rows:
        return 0
    embed = vocabulary_embedder()
    if embed is None:
        return 0
    done = 0
    for c in rows:
        try:
            with regions.writing_for(db, c.tenant_id, c.project_id), \
                    llm_usage.attributed(c.tenant_id):
                vec = embed(centroid_text(c))
        except Exception as e:  # noqa: BLE001 - the next pass tries again
            logger.warning("[vocabulary] centroid of %r not computed: %s", c.canonical,
                           log_safe.error(e))
            db.rollback()
            break
        if not vec:
            break
        done += _store_centroid(db, c, vec, still_missing=True)
    return done


def centroids_missing(db: Session) -> bool:
    """Whether some relation still has no centroid (`fill_missing_centroids`)."""
    return db.query(PredicateCluster.id).filter(PredicateCluster.centroid.is_(None)).first() is not None


def _store_centroid(db: Session, c, vec, *, still_missing: bool) -> int:
    """Writes `vec` only if the relation still has the example it was computed
    from (and, for the drain, still no centroid): the embedding call is slow,
    and an erasure committed meanwhile replaced the example and its centroid
    (store.examples_after_the_erasure). Writing the vector made from the
    erased value over it would keep a copy of what the erasure removed
    (embedding inversion, Morris et al., EMNLP 2023; the code audit of 18 Oct
    2026). Under READ COMMITTED Postgres re-reads the row the UPDATE
    waits on, so the check sees the erasure's commit; and an erasure still
    open holds the rows it scrubs from before it writes them to its commit
    (store.py), so this UPDATE waits for it. Returns 1 or 0."""
    q = db.query(PredicateCluster).filter(
        PredicateCluster.id == c.id,
        PredicateCluster.example_object.is_not_distinct_from(c.example_object))
    if still_missing:
        q = q.filter(PredicateCluster.centroid.is_(None))
    n = q.update({PredicateCluster.centroid: vec}, synchronize_session=False)
    db.commit()
    return 1 if n else 0


def recompute_centroids(db: Session, *, apply: bool) -> dict:
    """Every relation's centroid again, with the embedder in force: the first
    step of `manage recompute-vectors`, before the facts and the memories.
    First because a new phrase is compared with the centroids: against
    another model's, nothing is near, and the phrase makes a duplicate of a
    relation it should have joined.

    A relation whose call fails keeps the centroid it had; after
    `vectors.FAILURES_BEFORE_STOPPING` failures in a row the embedder is down
    and the rest keep theirs too (`stopped`). A relation with no centroid (its
    first phrase could not be embedded) gets one, and one whose example an
    erasure changed during the call keeps the erasure's (`_store_centroid`).
    Commits each."""
    from . import llm_usage
    from .vector_state import FAILURES_BEFORE_STOPPING

    rows = db.query(*_WHAT_A_CENTROID_IS_MADE_OF).order_by(PredicateCluster.created_at,
                                                           PredicateCluster.id).all()
    out = {"relations": len(rows), "recomputed": 0, "failed": 0, "stopped": False}
    embed = vocabulary_embedder() if apply else None
    if embed is None:
        return out
    in_a_row = 0
    for c in rows:
        try:
            with llm_usage.attributed(c.tenant_id):
                vec = embed(centroid_text(c))
        except Exception as e:  # noqa: BLE001 - a failed call keeps the old centroid
            logger.warning("[vocabulary] centroid of %r not recomputed: %s", c.canonical,
                           log_safe.error(e))
            out["failed"] += 1
            in_a_row += 1
            if in_a_row >= FAILURES_BEFORE_STOPPING:
                out["stopped"] = True
                return out
            continue
        in_a_row = 0
        out["recomputed"] += _store_centroid(db, c, vec, still_missing=False)
    return out


def resolve(
    db: Session,
    tenant_id: UUID,
    raw_predicate: str,
    *,
    object_label: str = "",
    project_id: Optional[UUID] = None,
    end_user_id: Optional[str] = None,
) -> Tuple[str, Optional[bool]]:
    """Map a raw relation phrase to its canonical form in the vocabulary of
    its end user of a project (`end_user_vocabulary`).

    Returns (canonical, is_functional). `is_functional` is None until the
    cluster has been classified; callers treat None as "not known to be
    single-valued" and leave both facts standing, which is the safe direction.
    Raises ResolutionUnavailable when the model was asked and never answered,
    or when an embedder is configured and its call failed.
    """
    r = resolve_relation(db, tenant_id, raw_predicate, object_label=object_label,
                         project_id=project_id, end_user_id=end_user_id)
    return r.canonical, r.is_functional


def resolve_relation(
    db: Session,
    tenant_id: UUID,
    raw_predicate: str,
    *,
    object_label: str = "",
    tense: Optional[str] = None,
    merge: bool = True,
    project_id: Optional[UUID] = None,
    end_user_id: Optional[str],
) -> _store.Relation:
    """`resolve`, plus the family of the relation (BUG-109, 2026-09-27) and the
    object its cardinality was measured on. `tense` "past" on an unseen phrase
    asks the model for its present form first (memory_core.store).
    `merge` False (memory_core 3.3.0, BUG-202): a predicate the caller chose
    (a fact written directly, a correction) keeps its name, reused when a
    relation or a variant of that name exists, never merged into another by
    the "same relation?" question."""
    return _store.resolve_predicate(
        db, _TABLES, end_user_vocabulary(tenant_id, project_id, end_user_id), raw_predicate, object_label or "",
        ask=_ask(), embed=vocabulary_embedder(), strict=True, tense=tense,
        # An answer that is none of those the question allows is asked again
        # (memory_core 2.2.0, twice, thinking) and then refuses the write
        # (ResolutionUnusable), instead of being cached as "a new relation" or
        # an "undecided" cardinality kept for ever: the road to I1. The hosted
        # product does the same since 2026-09-28.
        severe=True,
        # Whether a new relation holds one value is asked once and kept for
        # the vocabulary (one per end user of a project), so it is decided by
        # a majority of three (memory_core 2.1.0). One answer was a coin that
        # came up "many" for `subscribes_to` about once in five, and every
        # plan change in that vocabulary stayed open from then on (I1, blind
        # install of 0.1.13, when it was one per tenant).
        single_value_votes=_vocabulary.SINGLE_VALUE_VOTES,
        lazy_votes=LAZY_SINGLE_VALUE,
        merge=merge,
    )


def decide_if_undecided(db: Session, tenant_id: UUID, canonical: str, examples=(), *,
                        project_id: Optional[UUID] = None, end_user_id: Optional[str]):
    """(holds one value, its example) for `canonical`, voted now when the
    relation was born undecided (`LAZY_SINGLE_VALUE`): the same question, the
    same votes and the same model as at its birth, stored on its row
    (memory_core `store.decide_if_missing`). `examples`, the values a judge
    has in conflict, type the vote, each one asked (memory_core 3.3.0, 8 Oct:
    `reads` born with the example "reading" voted one value and closed every
    book with the next); without them, the birth example. Raises
    ResolutionUnavailable or ResolutionUnusable as the vocabulary does at
    birth: the write rolls back."""
    return _store.decide_if_missing(db, _TABLES, end_user_vocabulary(tenant_id, project_id, end_user_id), canonical,
                                 _ask(),
                                 severe=True, votes=_vocabulary.SINGLE_VALUE_VOTES, examples=tuple(examples or ()))


def reclassify(db: Session, tenant_id: UUID, *, apply: bool = False,
               project_id: Optional[UUID] = None, end_user_id: Optional[str]) -> dict:
    """Ask again, typed and by majority, whether each relation the vocabulary
    of one end user of a project has learned holds one value at a time
    (memory_core `reclassify_single_value`). A vocabulary whose first
    answer was wrong does not heal by itself: this is how an install that
    learned before 2.1.0 gets the new answer. Without `apply` nothing changes
    and the result says what would; with it the caller commits. Raises
    RuntimeError with no model key for the project's region (`_asked_in`)."""
    with _asked_in(db, tenant_id, project_id) as ask:
        return _store.reclassify_single_value(
            db, _TABLES, end_user_vocabulary(tenant_id, project_id, end_user_id), ask=ask, apply=apply,
        )


def review_variants(db: Session, tenant_id: UUID, *, apply: bool = False, votes: int = 3,
                    project_id: Optional[UUID] = None, end_user_id: Optional[str]) -> dict:
    """Ask again, one pair at a time, whether each phrase the vocabulary of one
    end user of a project learned as a variant of a relation is that relation
    (memory_core 3.3.0 `review_the_variants`, on since 8 Oct 2026). A phrase
    is merged at its birth and the answer kept for good:
    "visited" in `plans_to_visit`, "mentors" in `receives_support_from` stay
    wrong until asked again. Without `apply` nothing changes and the result
    says what would; with it a phrase the model separates is born under its
    own name and its facts move there, and the caller commits. The facts are the end user's of that project: the
    vocabulary is one per end user (`end_user_vocabulary`). `votes`: a phrase is
    separated only when a majority of that many answers say "different", the
    first "same" ending the asking (memory_core b882dad, after a reading of
    the 337 separations of the LoCoMo bench: about 87% right, the wrong ones
    near-synonyms and two forms of one phrase). Raises RuntimeError with no
    model key for the project's region (`_asked_in`)."""
    from ..db.models import AgentFact

    tables = _store.Tables(
        Fact=AgentFact, Alias=None, source="source_memory_id", sources="", alias_fact="",
        Relation=PredicateCluster, Variant=PredicateAlias,
    )
    with _asked_in(db, tenant_id, project_id) as ask:
        return _store.review_the_variants(
            db, tables, end_user_vocabulary(tenant_id, project_id, end_user_id), ask=ask,
            embed=vocabulary_embedder(), apply=apply,
            severe=True, single_value_votes=_vocabulary.SINGLE_VALUE_VOTES, lazy_votes=LAZY_SINGLE_VALUE,
            facts=and_(AgentFact.tenant_id == tenant_id,
                       AgentFact.project_id.is_not_distinct_from(project_id),
                       AgentFact.end_user_id.is_not_distinct_from(end_user_id)),
            votes=votes,
        )


@contextmanager
def _asked_in(db: Session, tenant_id: UUID, project_id: Optional[UUID] = None, *,
              stage: Optional[str] = None):
    """`_ask` for a question about a project's whole vocabulary, asked of
    the writer of that project's region (`regions.of_project`), never of
    whatever the install writes with by default (CONTRIBUTING.md: every door
    that calls a model reads its region; audit of 17 Oct 2026). Until the
    vocabulary was one per project (18 Oct 2026) it was the account's region,
    EU when any project wrote in EU. A write's own questions are asked inside
    its project's region already (facts/service.py, `regions.writing_for`).
    Raises RuntimeError when that region has no writer: nothing is asked of
    the other one."""
    from ..core import llm
    from . import regions

    region = regions.of_project(db, tenant_id, project_id)
    with llm.writing_in(region):
        ask = _ask(stage)
        if ask is None:
            where = (f"the region {region!r} this project's vocabulary is read in" if region
                     else "the install's default writer")
            raise RuntimeError(f"no model key for {where}: the question cannot be asked")
        yield ask


# The writer of a project's region for a question about one end user's
# names, as for the vocabulary (facts/objects.py, `manage review-objects`).
asked_in = _asked_in


def single_value(db: Session, tenant_id: UUID, canonical: str, *,
                   project_id: Optional[UUID] = None, end_user_id: Optional[str]):
    """(holds one value, the object it was measured on): typed functionality,
    memory_core.store.single_value, in the end user's vocabulary: the
    example the judge reads is a value of that end user's own facts."""
    return _store.single_value(db, _TABLES, end_user_vocabulary(tenant_id, project_id, end_user_id), canonical)
