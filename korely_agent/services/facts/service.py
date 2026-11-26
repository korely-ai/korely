"""Agent fact orchestration — extract+persist, direct write and correct.

The rules are the facts engine's (`services/memory_core/`). Extraction is
agent-tuned (extractor.py), contradiction is end_user-scoped
(contradiction.py), and predicate canonicalization and the family taxonomy
are memory_core's. The drain is drain.py, the reads reads.py and the forget
of one fact erasure.py, served here under their names of before.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm import Session

from . import closures
from .entities import named_by_the_caller
from .temporal import true_at
from ...core import llm as _llm
from ...core.times import aware
from ...core.llm import ModelDidNotAnswer, UnusableAnswer
from ...db.models import AgentFact, AgentMemory
from .. import llm_usage, regions as _regions
from .. import vector_state
from ..ids import fact_public_id, mem_public_id
from ..memory_core.store import ResolutionUnavailable, ResolutionUnusable
from ..memory_core import log_safe
from ..memory_write import SESSION

from ..memory_core.families import family_of_the_fact
from .contradiction import detect_and_invalidate
from .extractor import extract_facts_and_observation
# Forgetting and erasing live in erasure.py since the audit of 17 Oct 2026
# (this file was 1,990 lines), and are served here under their names of
# before: `facts.service.forget_memory`, `.forget_end_user`, `.delete_agent`.
from .erasure import (  # noqa: F401 - the names this module has always had
    erase_what_it_leaves, delete_agent, erase_memory,
    forget_end_user, forget_memory,
)
# The forget of one fact by hand, the same way since the 1.0.27 gate (this
# file was 1,011 lines): `facts.service.forget_fact`, `.ClosedBeforeItBegan`;
# with the one fact it reads (reads.load_fact) and the one-row test of being
# true now (temporal.true_now), which the correction below reads too.
from .erasure import ClosedBeforeItBegan, forget_fact  # noqa: F401
from .reads import load_fact
from .temporal import true_now
from . import previous_version
from .given_back import PAST, Held, record_by_hand
from ..memory_core.fact_time import effective_tense
# The drain, the same way, since the same audit: `facts.service
# .process_pending_memories` (the worker's job) and `.writers_refused` (/health).
from .drain import process_pending_memories, writers_refused  # noqa: F401
# And the reads, which write nothing: `facts.service.retrieve_agent_facts`,
# `.serialize_fact`, `.facts_for_memory`, `.memory_timeline`.
from .reads import (  # noqa: F401
    facts_for_memory, memory_timeline, retrieve_agent_facts, serialize_fact,
)
# The closing of a fact by the one that takes its place (superseding.py).
from .superseding import (  # noqa: F401 - the names this module has always had
    FactNotCurrent, ReplacesAnotherMemory, RestatedFactClosed, as_it_was_read, close_superseded,
    current_successor, fact_it_replaces,
)

logger = logging.getLogger(__name__)

# A fact write that needed a model and got no usable answer from it: the
# vocabulary, its embedding included (ResolutionUnavailable), or the judge
# (contradiction.JudgeUnavailable / JudgeUnreadable). Nothing was written; the
# doors that write a fact directly answer 503 `model_unavailable`
# (api/facts.py, api/admin.py), and a memory's write rolls back and is retried
# (`_persist_extracted`). core/llm.py says why, 2026-09-28. The embedding of
# the new fact is not one of them: the fact is kept and its vector retried
# (`_embed_fact`).
#
# ResolutionUnusable (the vocabulary answered, asked again, and never gave an
# answer its question allows) joined with strict reads, 0.1.15: a direct write
# answers 503 instead of 500, and a memory's write rolls back and ends 'error',
# visible in `manage status` and queued again by `reextract`, like an
# extraction whose answer cannot be read.
MODEL_FAILURES = (ResolutionUnavailable, ResolutionUnusable, ModelDidNotAnswer, UnusableAnswer)

# One judge call for a memory's write (memory_core `judge.batch`) instead of
# one per new fact, as the hosted product judges in production. On since
# 0.1.22, so that the two products judge alike: a replay of 59 LoCoMo
# closures (7 Oct, Gemini judge) closed 21 compatible facts by mistake in
# batches against 28 one by one, and a third of this
# engine's judge calls were a memory's second or later fact. The candidates
# are the per-fact ones; a past statement is still judged as it is written.
_BATCHED_JUDGE = True

# memory_core 3.3.0's event day (the hosted product's AGENTS_EVENT_DAY before
# it ran this code): a
# current fact of the events family starts on the one day the code dated in
# its own words ("yesterday" -> 2023-05-07) instead of the memory's day. On
# since 0.1.23 (8 Oct 2026): measured with the
# 3.3.0 pack and confirmations (LoCoMo, 7-8 Oct 2026, gpt-oss): with the lazy
# votes, the prefix and the event day, list-free, 610 answers of 762 against
# 618 of the same-day reading (inside noise), facts closed by mistake 53 -> 17,
# model calls per memory 2.84 -> 2.32.
_EVENT_DAY = True

# memory_core 6.19.0 (`writing._the_value_it_replaced`): a past statement
# read from a memory may end another value of its subject when two readings
# of the judge name it ("Tom downgraded from Gold to Basic" written as the
# event alone, with Gold left current). OFF: an engine switch that is off is turned
# on only after a measurement and the founder's yes. When on, only for the
# writer it was measured to be needed on, gpt-oss-120b (the Cloud's European
# region), by the rule that already chooses that writer's extraction rules
# (`extractor.measured_writer`): Gemini 3.1 Flash-Lite, the default region,
# wrote the new state in the present in 26 of 26 changes (18 Nov 2026), and
# there the path would only add calls.
_PAST_EVENT = False

# Every read, erasure and correction of this file takes `project_id` by name
# with no default (audit of 17 Oct 2026). Each defaulted to EVERY_PROJECT,
# "no wall", the opposite of what sentinels.py and `_persist_here` explain:
# /v1 and the MCP always passed one, /admin relied on the default, and a new
# door reached with a key that forgot it would have read or erased across
# the account's projects in silence. Forgetting it is a TypeError now; the
# operator's screen says EVERY_PROJECT.


def fact_text(subject_canonical, subject_label, predicate_raw,
              object_canonical, object_label) -> str:
    """The text a fact's vector is computed on: 'subject predicate object'.
    2026-09-27 — the CANONICAL names: a question about "Globex" must find
    the facts written when it was still called "Acme"."""
    return (
        f"{subject_canonical or subject_label} {predicate_raw} "
        f"{object_canonical or object_label}"
    ).strip()


def fact_vector(text: str):
    """The vector of a fact's text, or None when no embedder is configured.
    Raises `core.llm.EmbeddingFailed` when one is and the call gave no
    vector. The write and the worker (services/vectors.py) both go through
    here, so a vector computed later is the one the write would have got."""
    return _llm.embed_or_raise(text, task=_llm.TASK_DOCUMENT)


def _embed_fact(db: Session, tenant_id: UUID, fact: AgentFact) -> None:
    """Inline embedding of 'subject predicate object'. Does not commit.

    No embedder configured (no Google key): the fact is stored without a
    vector, `pending`, as it always was. With one, a call that gives no
    vector leaves the fact `pending` with the call counted, and the worker
    retries it (services/vectors.py: bounded, `error` after the last
    attempt, which `manage reextract` queues again). The write goes on.

    2026-09-28, twice. In the morning a failed call was made to refuse the
    whole write, because best effort had meant the fact stayed without a
    vector for good: nothing re-embedded a fact, and the vector is how every
    later fact's contradiction check (Stage 1b) and the context ranking find
    this one. The hosted engine chose instead to keep the fact and retry its
    vector through a durable job, and this is that choice,
    so that both products behave the same: a provider that fails embeddings
    for an hour no longer turns every fact write into a 503. What it still
    costs, on both: the write whose own vector failed is judged on the SQL
    candidates only, without the semantic ones."""
    text = fact_text(fact.subject_canonical, fact.subject_label, fact.predicate_raw,
                     fact.object_canonical, fact.object_label)
    try:
        emb = fact_vector(text)
    except _llm.EmbeddingFailed as e:
        logger.warning("[agent_facts] no vector for fact %s, the worker retries it: %s", fact.id,
                       log_safe.error(e))
        vector_state.missed(fact)
        return
    if emb:
        fact.embedding = emb
        fact.embedding_status = vector_state.DONE


def emit_invalidations(db: Session, tenant_id: UUID, write_shapes: List[dict]) -> None:
    """Enqueue a fact.invalidated webhook for each fact the write closed, in
    the caller's transaction: every caller calls it before the commit that
    closes the facts, so closings and events commit together (audit of 17
    Oct 2026; they were enqueued after it, and a failure in between kept the
    closing and lost its event). What the write shapes name first, closed by
    their fact; then every other fact this transaction closed, read from its
    row (closures.py: a past statement closes its true twin in place, and
    that closure was in no shape, functional test of 28 Oct 2026). One event
    per fact per transaction."""
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc).isoformat()
    for shape in write_shapes:
        for old_id in shape.get("invalidated", []):
            closures.announce(db, tenant_id, old_id, shape["id"], now)
    closures.announce_the_rest(db, tenant_id, now)


def _write_shape(fact: AgentFact, invalidated: List[UUID]) -> dict:
    """The shape returned inline on a write (POST /v1/memories facts[] and
    POST /v1/facts): the new fact + the ids it superseded."""
    return {
        "id": fact_public_id(fact.id),
        "subject": fact.subject_label,
        "predicate": fact.predicate_canonical,
        "object": fact.object_label,
        "predicate_family": family_of_the_fact(fact.predicate_family, fact.predicate_canonical),
        "valid_from": fact.valid_from.isoformat() if fact.valid_from else None,
        "invalidated": [fact_public_id(i) for i in invalidated],
        # 2026-09-27 — additive fields (the SDKs ignore unknown keys).
        "tense": fact.tense or "current",
        "invalid_at": fact.invalid_at.isoformat() if fact.invalid_at else None,
        "observation_count": int(fact.observation_count or 1),
        # 2026-10-07 — additive: whose fact it is, as the read shape says. The
        # Python SDK built its Fact from this shape and gave user_id None
        # (a new-customer test).
        "user_id": fact.end_user_id,
        "agent_id": fact.agent_id,
    }


def _persist_one(db: Session, tenant_id: UUID, *, project_id: Optional[UUID], **kw
                 ) -> tuple[AgentFact, List[UUID]]:
    """`_persist_here` with every model call (the contradiction judge, the
    vocabulary) sent to the writer of the project's region (8 Oct 2026,
    services/regions.py). Every door that writes a fact comes here."""
    with _regions.writing_for(db, tenant_id, project_id):
        return _persist_here(db, tenant_id, project_id=project_id, **kw)


def _persist_here(
    db: Session,
    tenant_id: UUID,
    *,
    end_user_id: Optional[str],
    agent_id: Optional[str],
    run_id: Optional[str],
    subject: str,
    subject_type: str,
    predicate_raw: str,
    obj: str,
    object_is_literal: bool,
    confidence: float,
    source_memory_id: Optional[UUID],
    model_used: Optional[str],
    project_id: Optional[UUID],
    valid_from: Optional[datetime] = None,
    first_memory_id: Optional[UUID] = None,
    tense: str = "current",
    text_valid_from: Optional[datetime] = None,
    arrival: Optional[datetime] = None,
    evidence: Optional[str] = None,
    defer: bool = False,
    day_of_the_words: Optional[datetime] = None,
    session_only: bool = False,
    valid_from_is_a_day: bool = False,
    held_by_hand=None,
) -> tuple[AgentFact, List[UUID]]:
    """Create one fact, run contradiction, embed. Caller commits.

    `defer`: write and embed a current fact, and leave its judgement to
    the caller's batched judge of the whole write (`_BATCHED_JUDGE`).

    `day_of_the_words` (memory_core 3.3.0, `ExtractedFact`, only with
    `_EVENT_DAY`): the one day the code dated in a current fact's own words
    ("yesterday" -> 2023-05-07). The writer starts a fact of the events family
    on that day; a state, and a past fact, keep the memory's time
    (`writing.write_fact`).

    `held_by_hand` (memory_core 6.14.0, `writing.write_fact`): given by the
    write of a memory (`_persist_extracted`), the triples of that memory the
    caller's last word says are over (`given_back.Held`), which its text
    written again does not undo.

    `evidence`: the words of the memory the fact was read from, as the
    extractor copied them (memory_core 2.5.0, `ExtractedFact.evidence`, kept
    only when they are in the text). The judge reads them under the new fact,
    so "also works part-time at Google" does not close "works at Apple"
    (BUG-200). They are not stored on the fact: a memory that is forgotten
    leaves no copy of its words behind. A fact written directly has none.

    `arrival`: when the text the fact was read from arrived (a memory's
    creation, or its last edit), the date the fact takes when neither the
    write's timestamp nor the text gives one. Without it that was the moment
    the worker got round to it, seconds later, and an `as_of` in between read
    the old value (the hosted product's audit, 2026-09-29, memory_core 2.3.0).
    A fact written directly is dated by its own request and passes none.

    `first_memory_id`: the memory whose words a row with no source is made
    of (a correction, `correct_fact`); None lets the database take the
    source (db/triggers.py).

    `project_id` has no default on purpose. It used to have one, and a default
    of None on a write means "no project", which on this schema means a row no
    key can ever read: invisible, not absent. One caller out of three forgot to
    pass it and nobody noticed until somebody corrected a fact by hand and
    watched it vanish. Required, so forgetting it is a TypeError at the call
    site instead of a row that quietly falls outside the wall.

    2026-09-27 (the same rules as the hosted product, from memory_core):
      * the relation is resolved against the tenant's learned vocabulary,
        strictly (ResolutionUnavailable fails the write);
      * subject and object get their CANONICAL names from the memory's
        aliases, and the same canonical triple already true is RECONFIRMED
        (last_confirmed_at, observation_count, source_memory_ids);
      * a past-tense statement closes the true twin instead of adding a row;
        with no twin it is stored as history, born closed;
      * `valid_from`: the caller's timestamp, else the date the text states,
        else now;
      * an explicit identity statement ("Acme is now called Globex") creates
        the alias by itself.
    """
    from . import entities as _ent
    from ..memory_core import writing
    from ..memory_core.fact_time import effective_tense
    from ..predicates import resolve_relation

    # The rules and their order are memory_core's (`writing.write_fact`,
    # piece 3 of the common engine, 0.1.15), shared with the hosted product:
    # aliases, reconfirmation, the past tense, the insert in a savepoint, the
    # identity alias, the vector before the judge, the judge. This engine
    # passes its memory, its columns, its vocabulary, its embedder and its
    # judge, and judges each fact as it is written, or leaves the current ones
    # to the batched judge of the write (`defer`, `_BATCHED_JUDGE`).
    # tests/test_common_engine_writing.py says what each write returns and
    # leaves in the store.
    tense = effective_tense(tense)

    # BUG-202: the words of a fact written directly or of a correction are
    # the caller's, not the extraction's.
    named = named_by_the_caller(model_used)

    return writing.write_fact(
        db, _ent.TABLES, _ent.memory_of(tenant_id, project_id, end_user_id, agent_id),
        columns=dict(tenant_id=tenant_id, project_id=project_id, end_user_id=end_user_id,
                     agent_id=agent_id, run_id=run_id, session_only=session_only,
                     first_memory_id=first_memory_id),
        # BUG-202 (memory_core 3.3.0): a predicate the caller chose, a fact
        # written directly or a correction, keeps its name; only what the
        # extraction read is asked "same relation?" (as the hosted product).
        relation=resolve_relation(db, tenant_id, predicate_raw, object_label=obj or "", tense=tense,
                                   merge=not named,
                                   project_id=project_id, end_user_id=end_user_id),
        is_open=true_at(),
        embed=lambda f: _embed_fact(db, tenant_id, f),
        judge=_judge_of_the_write(db, tenant_id, project_id=project_id,
                                  end_user_id=end_user_id, agent_id=agent_id, named=named,
                                  day=valid_from_is_a_day and valid_from is not None),
        subject=subject, subject_type=subject_type, predicate_raw=predicate_raw, obj=obj,
        object_is_literal=object_is_literal, confidence=confidence,
        source_memory_id=source_memory_id, model_used=model_used,
        valid_from=valid_from, text_valid_from=text_valid_from, tense=tense,
        arrival=arrival, defer=defer,
        # memory_core 2.5.0: the memory's words, for the judge only.
        evidence=evidence,
        day_of_the_words=day_of_the_words,
        # memory_core 6.14.0: a memory written again does not state again a
        # triple of its own the caller's last word says is over (given_back
        # .Held); None for a fact posted or corrected by hand.
        held_by_hand=held_by_hand,
        # memory_core 6.19.0 (`_PAST_EVENT`): only for a fact the extraction
        # read from a memory, written by the writer it was measured on.
        past_event=_past_event(source_memory_id, named, model_used, tense),
    )


def _judge_of_the_write(db: Session, tenant_id: UUID, *, project_id: Optional[UUID],
                        end_user_id: Optional[str], agent_id: Optional[str], named: bool,
                        day: bool = False):
    """The judge `writing.write_fact` calls on a fact of one write of
    `_persist_here` (`writing.ToJudge`): this engine's contradiction judge,
    inside the write's wall (memory_core 6.28.0: with no end user, the
    write's agent too)."""
    from ..memory_core import writing

    def judge(g: writing.ToJudge) -> List[UUID]:
        # Scoped, and this is the half that would be a silent leak rather than
        # a visible one: without the project a write in one project could
        # close a fact in another. Nobody would see a foreign row appear; they
        # would see one of theirs quietly stop being true.
        return detect_and_invalidate(
            db, tenant_id, project_id=project_id, end_user_id=end_user_id, agent_id=agent_id,
            new_fact_id=g.fact_id,
            new_subject=g.subject, new_subject_canonical=g.subject_canonical,
            new_predicate_raw=g.predicate, new_predicate_canonical=g.predicate_canonical,
            new_object=g.obj, new_object_canonical=g.object_canonical,
            new_embedding=g.vector, new_valid_from=g.when,
            functional=g.functional, example=g.example,
            new_past=g.past, source=g.source, evidence=g.evidence,
            # memory_core 6.5.0: a subject the caller named is weighed against
            # its own facts only (candidates.for_the_judge).
            subject_named=named,
            # memory_core 6.19.0: the readings of a change told as an event.
            among_its_subject=g.among_its_subject, ask_only=g.ask_only,
            # A valid_from given as a day wins over a fact begun later that
            # day, for the fact of this write (`contradiction._close_from_a_day`).
            new_valid_from_is_a_day=day and not g.past,
        )
    return judge


def _past_event(source_memory_id, named: bool, model_used: Optional[str], tense: str) -> bool:
    """Whether a past statement may end another value of its subject
    (`_PAST_EVENT`, memory_core 6.19.0): the switch on, a fact read from a
    memory (never one a caller posted or corrected: its words are the
    caller's), in the past, and the writer in force the one the extraction
    rules call gpt-oss (`extractor.measured_writer`, inside the region's
    writer, `_persist_one`)."""
    if not _PAST_EVENT or source_memory_id is None or named or tense != "past":
        return False
    from .extractor import measured_writer

    return measured_writer(model_used) == "gpt-oss"


# The memories the same end user wrote just before (earlier_memories.py).
from .earlier_memories import (  # noqa: F401,E402 - the names this module has always had
    PREVIOUS_MEMORIES, PREVIOUS_MEMORY_CHARS, previous_memories,
)


def extract_and_persist_facts(
    db: Session,
    tenant_id: UUID,
    *,
    memory,  # AgentMemory row
    model_name: Optional[str] = None,
    valid_from: Optional[datetime] = None,
    claim=None,
) -> List[dict]:
    """Mine one memory for facts, persist them, return the write-shape list.

    `claim` (memory_core 2.11.0, `worker.Claim`): the claim the memory
    is extracted under, the drain's or an inline write's. The model reads the
    claim's text (`claim.text`), never a second read of the row, and 'done'
    is written only if the row is still that claim's: a memory edited or
    forgotten during the model call keeps its new text, or its forgetting,
    and this write is discarded (`claim.outcome == "lost"`).

    `valid_from` is the event time the memory's facts became true (Graphiti
    bi-temporal valid_time, distinct from the ingestion/transaction time). When
    given, every extracted fact carries it — this is what makes `as_of`
    point-in-time queries correct (otherwise facts all carry ~now). Defaults to
    now when omitted.

    Resilient: on any extraction failure returns [] and leaves the memory's
    graph_sync_status='pending' for the async backfill worker. On success sets
    graph_sync_status='done'. Commits.

    The whole thing runs inside one usage scope, so every model call it makes
    (extraction, the contradiction judge, the predicate classifier, embeddings)
    is attributed to this memory and to the agent that wrote it. This is the
    choke point both write paths go through, the synchronous one and the
    drain, which is why the scope is here and not in each of them."""

    # And every call goes to the writer of the memory's project's region
    # (8 Oct 2026, services/regions.py), read now: a project that changed its
    # region writes there from its next memory.
    with llm_usage.attributed(
        tenant_id,
        agent_id=getattr(memory, "agent_id", None),
        memory_id=getattr(memory, "id", None),
        project_id=getattr(memory, "project_id", None),
    ), _regions.writing_for(db, tenant_id, getattr(memory, "project_id", None)):
        try:
            with llm_usage.stage(llm_usage.EXTRACTION):
                # memory_core 2.6.0: the facts and the observation in one
                # call, with the end user's memories before this one and its
                # day as context. memory_core 6.4.0: and whose memory it is,
                # the agent that wrote it read when it belongs to no end user.
                extracted = extract_facts_and_observation(
                    claim.text if claim is not None else memory.content,
                    end_user_id=memory.end_user_id,
                    db=db,
                    tenant_id=tenant_id,
                    model_name=model_name,
                    previous=previous_memories(db, memory),
                    date=_day(memory.event_time or valid_from or memory.created_at),
                    agent_id=getattr(memory, "agent_id", None),
                )
        except Exception as e:
            logger.warning("[agent_facts] extraction crashed for memory %s: %s", memory.id,
                           log_safe.stack(e))
            return []
        # Written with the facts, in their transaction: a write that rolls
        # back leaves the memory as it was. Always assigned, None included,
        # so an edited memory never keeps the sentence of its old text.
        memory.observation = extracted.observation
        # memory_core 2.9.0: the relative times and their dates, the same
        # way (None when the rule is off or the reply had none).
        memory.relative_times = extracted.times
        return _persist_extracted(
            db, tenant_id, memory=memory, extracted=extracted.facts,
            valid_from=valid_from, model_name=model_name, claim=claim,
        )


def extract_inline(db: Session, tenant_id: UUID, memory, *,
                   valid_from: Optional[datetime] = None) -> List[dict]:
    """The extraction of a write that waits for it (WRITE_ASYNC=false, the
    MCP door alike): the memory was written claimed (`memory_engine
    .extracting_now`), so the drain does not extract it at the same time. A
    failure gives it back to the queue, as the write's promise says ("left
    for the worker"); the drain's own failures are what files a memory as
    'error'. Returns the facts written."""
    # Through the package's name, the entry point every door called before
    # this helper existed (`facts.extract_and_persist_facts`).
    from .. import facts as _facts

    # The claim the row was born with, as memory_core 2.11.0 reads a claim:
    # the write closes 'done' on it, and a memory forgotten or edited between
    # the insert and here is somebody else's now.
    p = _worker.claim(db, memory)
    if p is None:
        return []
    mid = memory.id  # read now: an erased memory's expired row cannot be loaded again (a 500)
    try:
        facts = _facts.extract_and_persist_facts(db, tenant_id, memory=memory,
                                                 valid_from=valid_from, claim=p)
    except Exception as e:  # noqa: BLE001 - the write happened; the worker retries
        logger.warning("[agent_facts] inline extraction of %s failed, left to the worker: %s", mid, log_safe.stack(e))
        db.rollback()
        facts = []
    if p.outcome is None and _worker.release(db, p):
        set_committed_value(memory, "graph_sync_status", "pending")
        set_committed_value(memory, "graph_sync_claimed_at", None)
    return facts


def extraction_progress(db: Session) -> dict:
    """What the drain has done, read off the store: the oldest memory still
    waiting and the last extraction that answered (its usage row). For a
    /health whose process does not extract (RUN_WORKER=false, the worker in a
    process of its own), and for a drain that stopped with no failed call to
    show for it. Both reads are on indexes (`idx_agent_mem_graph_pending`, the
    usage rows' `ts`)."""
    from ...db.models import AgentLlmUsage

    oldest = (db.query(func.min(AgentMemory.created_at))
                .filter(AgentMemory.graph_sync_status == "pending").scalar())
    last = (db.query(func.max(AgentLlmUsage.ts))
              .filter(AgentLlmUsage.stage == llm_usage.EXTRACTION).scalar())
    return {"oldest_waiting": oldest, "last_extraction": last}


def _day(when: Optional[datetime]) -> Optional[str]:
    """The UTC day of a moment, YYYY-MM-DD, as the context dates it."""
    if when is None:
        return None
    return aware(when).astimezone(timezone.utc).date().isoformat()


def _persist_extracted(
    db: Session,
    tenant_id: UUID,
    *,
    memory,  # AgentMemory row
    extracted: list,  # List[ExtractedFact]
    valid_from: Optional[datetime] = None,
    model_name: Optional[str] = None,
    claim=None,
) -> List[dict]:
    """Persist already-extracted facts for ONE memory: dedup + embed + F3 (batched
    per-write or per-fact) + set graph_sync_status='done' + commit + emit webhooks.
    Returns the write-shape list.

    This is the persistence half of the write, shared verbatim by the synchronous
    inline path (extract_and_persist_facts) and the async Step-B batched worker — so
    the persisted facts + contradiction outcome are IDENTICAL no matter whether the
    facts came from a single-memory call or a batched cross-memory call. Resilient:
    on persist failure rolls back and returns [] (memory stays 'processing' → the
    worker marks it 'error', no infinite retry).

    2026-09-28 — a model the write needs that gives no usable answer is such
    a failure (MODEL_FAILURES): the judge with no verdict, the vocabulary
    that does not answer, the embedding call of the vocabulary that fails
    (a new fact's own vector is not: the fact is kept and its vector
    retried, `_embed_fact`). The whole write of the memory rolls back, facts and
    closures, and the facts already in the store stay as they were; a
    synchronous write leaves the memory 'pending' for the worker, whose one
    attempt ends 'done' or 'error'. Until today the judge's failure kept the
    new fact open next to the one it may contradict and marked the memory
    done, and a failed embedding cached a relation with no centroid."""
    # memory_core `worker.write_memory` (piece 4 of the common engine,
    # 0.1.15): the facts one by one through `_persist_one`, 'done' and one
    # commit, or a rollback of the whole memory and [] when a model the write
    # needs gives no usable answer. This engine keeps the columns of its facts
    # and the webhooks.
    from ..memory_core import worker
    from .objects import uniter

    arrived = worker.arrival(memory)
    # The webhooks of this write are enqueued from the engine's own hooks,
    # which it calls inside the write's transaction, before its commit:
    # `shape_of` for what each fact closed, `retire` for what the edit retired.
    # So they commit with the facts, or go with the rollback of a write that
    # failed or lost its claim (audit of 17 Oct 2026). They were enqueued
    # after the commit, each with a commit of its own.
    def shape_of(fact: AgentFact, invalidated: List[UUID]) -> dict:
        # Every fact this memory wrote or stated again passes here before the
        # commit: a note that states a session's fact makes it a note's, until
        # that note's source is detached (AgentFact.session_only, 18 Oct 2026).
        # A row held over (memory_core 6.14.0) is not stated again: it stays
        # as it is (a session's fact stays a session's).
        if memory.kind != SESSION and fact.id not in held.held:
            stated_by_a_note(fact)
        shape = _write_shape(fact, invalidated)
        emit_invalidations(db, tenant_id, [shape])
        return shape

    # What the new text does to the facts the previous one stated: the
    # copies it shared, weighed by the judge, and the rest retired after
    # (facts/previous_version.py).
    # `said` takes each statement as written, for who ended a row in place.
    shared, retire, said = previous_version.hooks(db, tenant_id, memory, arrived,
                                                    emit_retired=_emit_retired)
    # memory_core 6.14.0: a triple of
    # this memory whose last row the caller forgot, corrected or posted in
    # the past tense stays over when the memory is written again (given_back
    # .Held, read once, now). Such a row comes back from the write as it is,
    # and is not one of the facts this write wrote or restated: it is left
    # out of the answer's facts (PATCH answers it as `kept_forgotten`).
    held = Held(db, memory)

    def batch(new_facts):
        # The judge's model is the provider's, as for the per-fact judge
        # (`judge` in `_persist_one` passes none).
        from .contradiction import detect_and_invalidate_batch

        # memory_core 6.9.0: whose memory it is reaches the judge with each
        # fact (its end user); 6.28.0: and its agent, which with no end user
        # is the judge's wall (`contradiction._stage1_candidates`).
        return detect_and_invalidate_batch(db, tenant_id, new_facts=new_facts, project_id=memory.project_id)

    out = worker.write_memory(
        db, memory, extracted,
        write_one=lambda ef, defer: said(ef, _persist_one(
            db, tenant_id,
            end_user_id=memory.end_user_id,
            agent_id=memory.agent_id,
            run_id=memory.run_id,
            subject=ef.subject,
            subject_type=ef.subject_type,
            predicate_raw=ef.predicate_raw,
            obj=ef.object,
            object_is_literal=ef.object_is_literal,
            confidence=ef.confidence,
            source_memory_id=memory.id,
            model_used=model_name,
            valid_from=valid_from,
            tense=getattr(ef, "tense", "current"),
            text_valid_from=getattr(ef, "valid_from", None),
            # A fact belongs to the project of the memory it was mined
            # from. Nothing else would be defensible: the fact is a reading
            # of that sentence, and the sentence belongs to somebody.
            project_id=memory.project_id,
            arrival=arrived,
            evidence=getattr(ef, "evidence", None),
            defer=defer,
            day_of_the_words=getattr(ef, "day_of_the_words", None) if _EVENT_DAY else None,
            # Born of a work session, the fact is a session's until a note
            # states it too (`shape_of`, below).
            session_only=memory.kind == SESSION,
            # memory_core 6.14.0: a triple of this memory the caller's last
            # word says is over is not written again by its new text.
            held_by_hand=held,
        )),
        shape_of=shape_of,
        batch=batch if _BATCHED_JUDGE else None,
        retire=retire,
        shared=shared,
        claim=claim,
        # memory_core 6.1.0 (off, `objects.SAME_OBJECT`): a new object the
        # writer says is the same thing as one of the memory's ("Italy" and
        # "Italia") is united before the facts are written.
        objects=uniter(db, tenant_id, memory, model_name=model_name,
                       text=claim.text if claim is not None else memory.content),
    )
    if claim is not None and claim.outcome == "done":
        # The compare-and-set wrote 'done' in the database, not on this
        # object; told without making it dirty, so a later flush cannot
        # write it again over a forgetting that came after.
        set_committed_value(memory, "graph_sync_status", "done")
    return [shape for shape in out if shape["id"] not in {fact_public_id(i) for i in held.held}]


def _emit_retired(db: Session, tenant_id: UUID, facts: List[AgentFact], memory_id: UUID) -> None:
    """fact.invalidated for the facts an edit of their memory no longer states.
    No new fact closed them, so `invalidated_by` is null and `reason` says why,
    as the hosted product sends it."""
    for f in facts:
        closures.announce(db, tenant_id, fact_public_id(f.id), None,
                          f.invalid_at.isoformat() if f.invalid_at else None,
                          reason="memory_updated", memory_id=mem_public_id(memory_id))


# The event time of a write and the order of the drain: memory_core
# `worker` (piece 4 of the common engine, 0.1.15), under the names the API,
# the batch import and the MCP already call. One parser for every write path
# that carries a `timestamp` (POST /v1/memories, each /v1/batch item, the MCP
# korely_add); on the request path a value it cannot read is a 422, never a
# silent "now" (services/memory_write.require_event_time). What it parses is stored on
# the row (`agent_memories.event_time`), which is where the worker reads it:
# never the developer's metadata. The drain (drain.py) writes the later
# episode after the earlier, by `worker.order_key`.
from ..memory_core import worker as _worker  # noqa: E402

parse_event_time = _worker.read_event_time
event_time_of = _worker.event_time_of


def stated_by_a_note(fact: AgentFact) -> None:
    """A fact a note (`shape_of`) or the developer (`write_fact_direct`,
    `correct_fact`) states: no longer `session_only` (AgentFact; audit of 20
    Oct 2026)."""
    if fact.session_only:
        fact.session_only = False


class RelationSaysNothing(ValueError):
    """A relation a caller gave whose canonical form is `unknown`
    (predicate_forms.canonicalize_predicate: one or two letters of a script
    with case, "is", "è"; one of a script without case, Chinese, passes).
    Written, every such fact of a subject read as one relation and the judge
    closed one with another (finding of 6 Nov 2026). 422, nothing written."""


RELATION_SAYS_NOTHING = ("Give a relation that says what it is (has_license, costs, works_at): "
                         "one of one or two letters, as \"is\", \"è\" or \"ha\", names none, "
                         "and nothing was written.")


def check_relation(predicate: Optional[str]) -> None:
    """Raise RelationSaysNothing for a relation that names none; None (a
    correction that keeps it) passes. The store's own reading decides."""
    from ..memory_core.predicate_forms import canonicalize_predicate

    if predicate is not None and canonicalize_predicate(predicate) == "unknown":
        raise RelationSaysNothing(RELATION_SAYS_NOTHING)


@llm_usage.billed_to_the_account
def write_fact_direct(
    db: Session,
    tenant_id: UUID,
    *,
    subject: str,
    predicate: str,
    obj: str,
    end_user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    run_id: Optional[str] = None,
    subject_type: str = "unknown",
    object_is_literal: bool = False,
    confidence: float = 0.9,
    valid_from: Optional[datetime] = None,
    project_id: Optional[UUID] = None,
    tense: str = "current",
    valid_from_is_a_day: bool = False,
    replaces: Optional[UUID] = None,
) -> Optional[dict]:
    """POST /v1/facts — write a triple directly (no extraction), run the
    contradiction check, embed. Commits and returns the write shape. `tense`
    is said by the writer (the store does not guess it from the verb).
    Raises one of MODEL_FAILURES, with nothing committed, when a model the
    write needs gives no usable answer; the caller rolls back, and
    RelationSaysNothing, before anything, for a relation that names none.

    `replaces`: the fact this one takes the place of, said by the caller
    (until 1.0.40, "LoCoMo has_score 86.9%" posted
    beside "93.6%" left both true when no judge closed it). It is closed
    where the new fact begins, `invalidated_by` the new one, and kept in the
    history, as a correction closes the fact it corrects (`correct_fact`,
    Mem0's update of a memory by its id, here without losing the old
    value). Checked before anything is written (`fact_it_replaces`): None when it
    is not in this project, ReplacesAnotherMemory when it is another end
    user's or agent's memory, or the new fact is not a current one,
    FactNotCurrent when it is history, ClosedBeforeItBegan when the new
    fact begins before it."""
    check_relation(predicate)
    old = None
    if replaces is not None:
        old = fact_it_replaces(db, tenant_id, replaces, project_id=project_id,
                        end_user_id=end_user_id, agent_id=agent_id, tense=tense,
                        valid_from=valid_from, valid_from_is_a_day=valid_from_is_a_day)
        if old is None:
            return None
        as_read = as_it_was_read(old)
    fact, invalidated = _persist_one(
        db, tenant_id,
        end_user_id=end_user_id,
        agent_id=agent_id,
        run_id=run_id,
        subject=subject,
        subject_type=subject_type,
        predicate_raw=predicate,
        obj=obj,
        object_is_literal=object_is_literal,
        confidence=confidence,
        source_memory_id=None,
        model_used="direct",
        valid_from=valid_from,
        project_id=project_id,
        tense=tense,
        valid_from_is_a_day=valid_from_is_a_day,
    )
    stated_by_a_note(fact)
    if effective_tense(tense) == "past" and not closures.born_here(db, fact):
        # The caller says by hand that a fact it held is over: its record,
        # of a scope of its own (not a forget), so no deletion gives it back
        # (facts/given_back.py).
        record_by_hand(db, tenant_id, fact, invalidated=int(closures.closed_here(db, fact)),
                       scope=PAST)
    if old is not None and fact.id != old.id:
        # Where the new fact begins, never before the old one did (a fact is
        # never closed before it begins): the new value restating a fact
        # already true from earlier retracts the one it replaces.
        begins = aware(fact.valid_from) if fact.valid_from is not None else \
            datetime.now(timezone.utc)
        if old.valid_from is not None:
            begins = max(begins, aware(old.valid_from))
        invalidated = close_superseded(db, tenant_id, old, fact, begins, invalidated,
                                  as_read=as_read)
        if invalidated is None:
            return None
    # The same triple, still true, is the fact restated (reconfirmed): it
    # replaces nothing, as a correction to the value it has (`correct_fact`).
    shape = _write_shape(fact, invalidated)
    emit_invalidations(db, tenant_id, [shape])
    db.commit()
    return shape


def correction_fields(subject: Optional[str], predicate: Optional[str],
                      obj: Optional[str]) -> Optional[dict]:
    """The fields of a correction as `correct_fact` takes them, stripped, a
    blank one left out so it keeps the old value (not a subject made of
    spaces); None when none is given. One reading for the two doors that
    correct a fact, `/v1` and the operator's screen (the code audit of
    2026-10-06, row 15)."""
    fields = {"subject": (subject or "").strip() or None,
              "predicate": (predicate or "").strip() or None,
              "obj": (obj or "").strip() or None}
    return fields if any(fields.values()) else None


@llm_usage.billed_to_the_account
def correct_fact(
    db: Session,
    tenant_id: UUID,
    fact_uuid: UUID,
    *,
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    obj: Optional[str] = None,
    project_id: Any,
    write_shape: bool = False,
) -> Optional[dict]:
    """Correct a fact = supersede, not mutate. Writes a NEW fact with the
    corrected fields (carrying the old one's end_user/agent/run, subject_type
    and, for an inherited subject, the extraction's `model_used`; no source
    memory: the caller's statement; as its first writer the memory the old
    one was stated by, whose erasure takes it) then invalidates the old one,
    linking old.invalidated_by → new.
    Both stay visible in time-travel; only the new one is current. None if
    the original isn't found. Raises FactNotCurrent, with nothing written,
    when the fact is history: only the truth as it stands is corrected.

    Returns the new fact's read shape (the dashboard), or with `write_shape`
    the write shape of POST /v1/facts, whose `invalidated` lists every fact
    this correction superseded: the corrected one, plus any other the
    contradiction pass on the new fact closed. PATCH /v1/facts/{id} answers
    that shape, and until 2026-09-28 it answered `invalidated: []` although it
    had just closed the fact it was asked to correct: the read shape went
    through the write model, which has no field to carry it. Each id listed
    gets its `fact.invalidated` webhook, as on the write path.

    Raises one of MODEL_FAILURES, with nothing committed, when the new fact
    could not be resolved or judged; the caller rolls back and
    answers 503 (it was a 500 on PATCH /v1/facts until 2026-09-28).
    RelationSaysNothing, before anything, for a relation that names none."""
    from datetime import datetime as _dt, timezone as _tz
    check_relation(predicate)
    old = load_fact(db, tenant_id, fact_uuid, project_id=project_id)
    if old is None:
        return None
    # The corrected fact is written in the old one's project, which a
    # correction read across projects (the dashboard's) names only here.
    llm_usage.in_project(old.project_id)

    now = _dt.now(_tz.utc)
    if not true_now(old, now):
        raise FactNotCurrent(fact_public_id(old.id), current_successor(db, old, now))
    as_read = as_it_was_read(old)
    # Where the corrected value begins: now, or where the old one was to begin
    # when that is still ahead (a contract that starts next month, corrected
    # today). A fact is never closed before it begins (CONTRIBUTING.md, as
    # `_invalidate_facts` and the judge do), and the correction does not make
    # the new value true before the old one would have been (audit of 17 Oct
    # 2026: both happened, and the old row was true on no date at all).
    begins = max(now, aware(old.valid_from)) if old.valid_from is not None else now
    # Whose words the corrected fact carries (memory_core 6.6.0, the decision of
    # 7 Nov 2026): the subject is the caller's when the correction names it, or
    # when the corrected fact was already the caller's (a fact written directly,
    # an earlier correction), and the row is a correction. A correction that
    # gives only the relation or the object of a fact the extraction read keeps
    # that fact's subject as the extraction wrote it, and its provenance: it is
    # judged as an extracted fact, every stage and every name the extraction
    # gives one entity, and its inherited relation is resolved as the
    # extraction's (`merge`). `model_used` is read only as this provenance
    # (`entities.named_by_the_caller`, `NAMED_FACTS`, objects.py).
    caller_named = subject is not None or named_by_the_caller(old.model_used)
    # No source memory: the caller's statement, as `write_fact_direct`'s. With
    # the corrected fact's memory (up to 1.0.26) every rule asking which facts
    # a memory said (memory_core `store.source_condition`) counted it: an edit
    # of that memory retired it, its forget closed it, and the corrected fact
    # stayed closed (code review finding, 15 Nov 2026). A source takes only
    # what it created, as Graphiti's `remove_episode` deletes "only edges
    # created by the episode" (none added by hand).
    # But its first writer stays that memory, as the database recorded it up
    # to 1.0.26 (db/triggers.py, from the source at the insert), or, when the
    # old row is itself one no memory states (an earlier correction), the
    # memory its words came from: the row is made of the old one's words, so
    # that memory's erasure deletes it as before, and nothing an erasure took
    # is kept by this change (facts/erasure.erase_memory, the review of the
    # same finding). A fact posted by hand has none.
    new_fact, closed_by_contradiction = _persist_one(
        db, tenant_id,
        end_user_id=old.end_user_id,
        agent_id=old.agent_id,
        run_id=old.run_id,
        subject=(subject or old.subject_label),
        subject_type=old.subject_type,
        predicate_raw=(predicate or old.predicate_raw),
        obj=(obj or old.object_label),
        object_is_literal=old.object_is_literal,
        confidence=old.confidence if old.confidence is not None else 0.9,
        source_memory_id=None,
        first_memory_id=old.source_memory_id or old.first_memory_id,
        model_used="correction" if caller_named else old.model_used,
        valid_from=begins,
        # The one field the correction used to forget. Without it the new fact
        # landed outside every project, so no key could reach it: a human
        # correcting a fact did not change the truth, it made the truth
        # disappear, leaving the old value closed and no replacement visible.
        project_id=old.project_id,
    )
    stated_by_a_note(new_fact)  # a correction is a direct statement
    if new_fact.id == old.id:
        # The "correction" names the fact as it already stands (the same
        # canonical triple, still true): _persist_one reconfirmed it, as a
        # POST /v1/facts of the same triple would. Closing it now would point
        # it at itself and leave nothing true (it did, until 2026-09-28), so
        # the fact stays open and nothing was superseded.
        db.commit()
        return _write_shape(new_fact, []) if write_shape else serialize_fact(new_fact)
    # Explicitly supersede the corrected fact (the contradiction pass on the new
    # fact may not catch a same-predicate value change): the fact that closes
    # it now is the correction (`close_superseded`).
    superseded = close_superseded(db, tenant_id, old, new_fact, begins, closed_by_contradiction,
                             as_read=as_read)
    if superseded is None:
        return None
    shape = _write_shape(new_fact, superseded)
    # Enqueued in the correction's transaction, which commits both.
    emit_invalidations(db, tenant_id, [shape])
    db.commit()
    return shape if write_shape else serialize_fact(new_fact)
