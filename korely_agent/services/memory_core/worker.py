"""The worker that drains an Agents engine's pending memories (memory_core,
piece 4 of the shared-code plan, 2026-09-28).

Both engines queue a memory ('pending') and extract it later, or extract it
inline and leave it 'pending' when a model gives no usable answer. The steps
of the drain were written twice, identical in code and not in comments, with
one real difference: the self-hosted claimed with `FOR UPDATE SKIP LOCKED`,
the cloud did not, so two drains running at once (a second replica, a drain
script started by hand) both claimed and both extracted the same memory: two
model calls billed for one memory, two passes of the judge.

What is here:
  * the event time of a memory and the order of the drain: the later episode
    after the earlier, so the judge sees the earlier fact and closes it at the
    right date (Graphiti §2.2.3, bi-temporal valid time);
  * the claim, with the reaper of rows left 'processing' by a crash;
  * the claim token (`Claim`, 2.11.0): every way out of 'processing' is a
    compare-and-set on what the worker read when it took the memory;
  * the write of one memory's extracted facts, per fact or with the whole
    write judged in one call (`batch`, an option still to measure);
  * the drain of one memory: 'done', or 'error' after its one attempt; and
    the inline extraction of a synchronous write, under the same claim.
What stays in the engine: the model calls, the spend pause and billing
(cloud), usage attribution (self-hosted), threads and batched extraction.

The claim token (2.11.0, code audit of 6 Oct). The write of a memory
closed it with `graph_sync_status = 'done'`, whatever had happened to the
memory meanwhile. A memory forgotten after the POST and before the drain got
to it was claimed and extracted anyway: the model paid, its facts open and
returned by every read. A PATCH during the extraction left the facts of the
OLD text and the status 'done', and the new text was never extracted. The
same class as an earlier pipeline's BUG-101, closed there with a claim
token. Here the token is what the worker
read of the row when it took it: the claim's time, the status, the text it
sends to the model, and that the row is not forgotten. The write ends with
`UPDATE ... WHERE` all of that still holds: optimistic concurrency, the
read phase without locks and a validation before the write is made visible
(Kung and Robinson, "On Optimistic Methods for Concurrency Control", ACM
TODS 6(2), 1981). A write that fails the check rolls back, facts and
closings: it was never true of the memory as it is now. The same check
guards the other ways out of a claim ('error', and back to 'pending').
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, func, inspect, or_, select
from sqlalchemy.orm.exc import StaleDataError

from . import store as _store
from . import log_safe as _log

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# The status of a forgotten memory (2.11.0). Terminal: the claim takes only
# 'pending', the reaper only 'processing'. Without it a memory forgotten while
# it waited stayed 'pending' for ever: never claimed once the claim leaves the
# forgotten out, but counted in every backlog that reads the status.
FORGOTTEN = "forgotten"


def read_event_time(s: Optional[str], now: Optional[datetime] = None) -> Optional[datetime]:
    """The `timestamp` of a memory write (when its events occurred) as the
    `valid_from` its facts inherit. ISO date or datetime; date-only means
    midnight UTC; naive means UTC. None when absent or unparseable: the
    request paths answer 422 for a value they cannot read, because putting
    "now" in place of a date the caller gave is a silent wrong answer to every
    `as_of` question about that period.

    A time later than the moment the write is read (`now`) is read as
    that moment (3.1.5, 7 Oct 2026): what a memory says cannot have been said
    after it arrived, and a fact valid from a later instant is not true now,
    so a client clock three minutes ahead hid the memory from every read of
    the present (/v1/context empty, the customer audit of that day) and a
    2030 timestamp hid it until 2030. The choice of 6 Oct among
    the three ways (a 422, back to now, left as it is): never a 422. A fact
    that begins later is written with its own date (`valid_from` of a fact
    write), not through a memory's timestamp."""
    if not s or not isinstance(s, str):
        return None
    raw = s.strip().replace("Z", "+00:00")
    dt = None
    for candidate in (raw, raw + "T00:00:00"):
        try:
            dt = datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    now = now or datetime.now(timezone.utc)
    return min(dt, now)


def event_time_of(m) -> Optional[datetime]:
    """The event time a write stored on its memory (`event_time`), aware, or
    None when the write gave none. SQLite hands back a naive value; the
    column is UTC."""
    ev = getattr(m, "event_time", None)
    if ev is None:
        return None
    return ev if ev.tzinfo else ev.replace(tzinfo=timezone.utc)


def arrival(m) -> Optional[datetime]:
    """When the memory's CURRENT text arrived, aware: its last edit
    (`updated_at`, which moves only with a real edit) when there was one, else
    its creation. The date its facts take when neither the write nor the text
    gives one (`writing.write_fact(arrival=)`, 2.3.0)."""
    def _aware(v):
        if v is None:
            return None
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)

    created = _aware(getattr(m, "created_at", None))
    modified = _aware(getattr(m, "updated_at", None))
    if modified is not None and (created is None or modified > created):
        return modified
    return created


def event_key(m) -> datetime:
    """Sort key of the drain: the event time, else the ingestion time, else
    the epoch. When two pending memories carry a knowledge update
    (Acme -> Globex), the later episode is written after the earlier, so the
    judge sees the earlier fact and closes it at the later one's date.
    Normalized to aware so the sort never mixes naive and aware."""
    ev = event_time_of(m)
    if ev is not None:
        return ev
    ca = getattr(m, "created_at", None)
    if ca is not None:
        return ca if getattr(ca, "tzinfo", None) else ca.replace(tzinfo=timezone.utc)
    return _EPOCH


def order_key(m) -> tuple:
    """`event_key`, then the order of writing: the sort key of a group the
    drain mines. Two memories with the same event time (a date-only or a
    repeated `timestamp`: the product's own import path) are mined in the
    order they were written, never by their random id, so the judge meets the
    earlier statement first. Graphiti keeps two timelines for this reason:
    when it happened (T) and when the system learned it (T'), and the second
    orders what the first cannot (Rasmussen et al., 2025, section 2.2.3).
    The id comes last, a total order for rows written in the same instant
    (audit of 27 Oct 2026)."""
    ca = getattr(m, "created_at", None)
    if ca is not None and getattr(ca, "tzinfo", None) is None:
        ca = ca.replace(tzinfo=timezone.utc)
    return (event_key(m), ca if ca is not None else _EPOCH, str(getattr(m, "id", "")))


def event_order(M: Any):
    """SQL form of `event_key`: the event time, else the ingestion time
    (`coalesce(event_time, created_at)`; the ingestion time alone for a model
    with no `event_time`)."""
    ev = getattr(M, "event_time", None)
    return func.coalesce(ev, M.created_at) if ev is not None else M.created_at


def claim_pending(db, M: Any, *, limit: int, expiry_minutes: int, filter_condition: Any = None,
           person: Any = None, per_person: Optional[int] = None) -> list:
    """Claim up to `limit` pending memories of model `M`, earliest event
    first, and commit the claim. Before that, the reaper: a row left 'processing' longer
    than `expiry_minutes` (or with no claim time: nobody claimed it with this
    code) goes back to 'pending'.

    `filter_condition` (2.7.0): one more condition on the rows to claim, from the engine
    that calls. The cloud leaves out the accounts whose monthly AI budget is
    used up: their memories wait for the next month and never take the turn
    of the accounts that still have one (oldest first, a waiting backlog at
    the head of the queue would hold everybody else back).

    `person` and `per_person` (2.10.0, off when not given): at most
    that many memories of each person per claim, each person's oldest first.
    `person` is the engine's columns that name a person (the cloud
    `(user_id, end_user_id)`, the self-hosted `(tenant_id, end_user_id)`).
    Oldest first alone gives the whole claim to whoever imported last: on
    LoCoMo whole (6 Oct, korely-agent 6e795ce) 5,682 memories were
    queued and the 20 oldest were all of the first conversation's two
    people, so everybody else waited with nothing being done for them.
    Capped per person, a burst from one sender cannot take the turn of the
    others: the fair queueing of Demers, Keshav and Shenker (SIGCOMM 1989,
    "Analysis and Simulation of a Fair Queueing Algorithm"), where each flow
    gets its share and a flow that sends faster only queues behind itself.
    The order inside a person never changes (the judge needs the earlier
    episode first). A claim shorter than `limit` with the queue not empty
    is the cap at work, not an empty queue: the engine claims again.

    The order is the event time, else the ingestion time (`event_order`,
    2.12.0; code audit of 6 Oct), as the drain already sorts each
    claimed group (`event_key`). It was the ingestion time alone: a
    /v1/batch exported newest first, with five memories per person per
    claim, put "works at Globex" (2025-06) in an earlier claim than "works at
    Acme" (2024-01), so the judge read the older fact after the newer one.
    The cap per person is unchanged (at most N each, each person's earliest
    first); across people the claim now goes by event time as well. It is
    the cheap half of the fix: a memory with an older event can still arrive
    after its later ones were written, and `judge.close` keeps the store
    right in that case.

    `FOR UPDATE SKIP LOCKED` while selecting: without it two drains select the
    same rows, both write 'processing', both commit and both extract. Skip,
    not wait: the other drain already has them. The commit releases the locks;
    from then on the status keeps the rows to this drain. The claim time is
    the reaper's clock (`graph_sync_claimed_at`), never the developer's
    `updated_at`.

    A forgotten memory (`deleted_at` set) is never claimed (2.11.0): until
    then a memory deleted before the drain reached it (with deferred writes, a
    queue, a spending pause, a provider down) was extracted anyway, the model
    paid, and its facts came back in every read. The reaper lays to rest
    (`FORGOTTEN`) the forgotten rows still 'pending' or 'processing', which
    an engine's forget wrote before 2.11.0 or without the status, instead of
    putting them back in the queue."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=max(1, int(expiry_minutes)))
    alive = _not_forgotten(M)
    reclaimed = (
        db.query(M)
        .filter(M.graph_sync_status == "processing",
                or_(M.graph_sync_claimed_at.is_(None), M.graph_sync_claimed_at < cutoff),
                alive)
        .update({M.graph_sync_status: "pending"}, synchronize_session=False)
    )
    buried = 0
    if hasattr(M, "deleted_at"):
        buried = (
            db.query(M)
            .filter(M.graph_sync_status.in_(("pending", "processing")), M.deleted_at.isnot(None))
            .update({M.graph_sync_status: FORGOTTEN}, synchronize_session=False)
        )
    if reclaimed or buried:
        db.commit()
        if reclaimed:
            logger.info("[memory] reclaimed %d stale 'processing' memories -> pending", reclaimed)
        if buried:
            logger.info("[memory] %d forgotten memories left the queue", buried)

    to_take = db.query(M).filter(M.graph_sync_status == "pending", alive)
    if filter_condition is not None:
        to_take = to_take.filter(filter_condition)
    if person is not None and per_person:
        columns = list(person) if isinstance(person, (list, tuple)) else [person]
        # Event time, then the time the system learned it, then id: Graphiti keeps two
        # times, T (when it happened) and T' (when it was learned), and the second
        # orders what the first cannot (Rasmussen et al., 2025, section 2.2.3).
        rank = func.row_number().over(partition_by=columns, order_by=(event_order(M).asc(), M.created_at.asc(), M.id.asc()))
        queued = select(M.id.label("id"), rank.label("rank")).where(M.graph_sync_status == "pending", alive)
        if filter_condition is not None:
            queued = queued.where(filter_condition)
        queued = queued.subquery()
        to_take = to_take.filter(
            M.id.in_(select(queued.c.id).where(queued.c.rank <= int(per_person))))
    taken = (
        to_take
        # Event time, then the time the system learned it, then id: Graphiti keeps two
        # times, T (when it happened) and T' (when it was learned), and the second
        # orders what the first cannot (Rasmussen et al., 2025, section 2.2.3).
        .order_by(event_order(M).asc(), M.created_at.asc(), M.id.asc())
        .limit(max(1, int(limit)))
        .with_for_update(skip_locked=True)
        .all()
    )
    if not taken:
        db.commit()
        return []
    for m in taken:
        m.graph_sync_status = "processing"
        m.graph_sync_claimed_at = now
    db.commit()
    return taken


def _not_forgotten(M):
    """SQL: the memory is not forgotten (no condition for a model without
    `deleted_at`)."""
    from sqlalchemy import true

    return M.deleted_at.is_(None) if hasattr(M, "deleted_at") else true()


# ---------------------------------------------------------------------------
# the claim token (2.11.0)
# ---------------------------------------------------------------------------

@dataclass
class Claim:
    """A worker's claim on one memory, and what it read of the row when it
    took it: the token every way out of 'processing' is checked against
    (module docstring). `text` is the text the model reads: an engine sends
    the model `claim.text`, never a second read of the row, so the facts it
    writes are the facts of the text the check holds.

    `outcome` says how the claim ended, for the engine that needs to know which
    of two empty results it got: "done" (written), "lost" (the memory
    changed under the write, which was discarded), "error", "pending" (given
    back to the queue); None while it is open."""
    M: Any
    id: Any
    when: Optional[datetime]
    text: Optional[str]
    outcome: Optional[str] = None


def claim(db, m) -> Optional["Claim"]:
    """The claim a drain holds on `m`, read fresh from the database (one
    query, so status, claim time and text are one snapshot of the row): None
    when the row is not 'processing' or is forgotten, i.e. not a claim any
    more (edited, forgotten, closed or given back meanwhile)."""
    M = type(m)
    columns = [M.graph_sync_status, M.graph_sync_claimed_at, M.content]
    if hasattr(M, "deleted_at"):
        columns.append(M.deleted_at)
    row = db.query(*columns).filter(M.id == m.id).first()
    if row is None or row[0] != "processing" or (len(row) > 3 and row[3] is not None):
        return None
    return Claim(M, m.id, row[1], row[2])


def still_its_own(p: Claim):
    """SQL: the row is still the one this claim took. 'processing', the same
    claim time, the same text, not forgotten. A PATCH puts the row back to
    'pending' with a new text, a forget sets `deleted_at` (and
    `FORGOTTEN`), a second drain that took it after the reaper stamps
    another time: each makes this false."""
    M = p.M
    return and_(
        M.id == p.id,
        M.graph_sync_status == "processing",
        M.graph_sync_claimed_at.is_(None) if p.when is None else M.graph_sync_claimed_at == p.when,
        M.content.is_(None) if p.text is None else M.content == p.text,
        _not_forgotten(M),
    )


def close_the_claim(db, p: Claim, status: str, **columns) -> bool:
    """The compare-and-set: `UPDATE ... SET graph_sync_status = status (and
    `columns`) WHERE still_its_own(p)`. True when the row was still this claim's
    and is now `status`. Does not commit."""
    values = {p.M.graph_sync_status: status}
    for name, value in columns.items():
        values[getattr(p.M, name)] = value
    n = db.query(p.M).filter(still_its_own(p)).update(values, synchronize_session=False)
    if n:
        p.outcome = status
    return bool(n)


def release(db, p: Optional[Claim]) -> bool:
    """Back in the queue ('pending', no claim time) a memory this claim took
    and did not close: a pause, a provider that refused, an inline write that
    failed. A no-op for a memory that is no longer this claim's (written,
    edited, forgotten, taken by another drain). Commits. True when it gave
    the memory back."""
    if p is None:
        return False
    fact = close_the_claim(db, p, "pending", graph_sync_claimed_at=None)
    db.commit()
    return fact


def requeue(db, rows: Sequence) -> int:
    """Back in the queue the memories a drain claimed and did not work (the
    rest of a group, when the provider rests). Only those still 'processing'
    and not forgotten: until 2.11.0 the engines wrote 'pending' on the
    objects in their session, which put back in the queue a memory forgotten
    meanwhile (its forget overwritten) and gave a claim to nobody. The claim
    time of a row not yet worked is not checked: a claim the reaper took back
    and another drain took again in between would be released too, and that
    drain's own check then discards its work and the memory goes back to the
    queue (time lost, never a wrong write). Commits. Returns how many."""
    if not rows:
        return 0
    M = type(rows[0])
    n = (
        db.query(M)
        .filter(M.id.in_([m.id for m in rows]), M.graph_sync_status == "processing", _not_forgotten(M))
        .update({M.graph_sync_status: "pending", M.graph_sync_claimed_at: None}, synchronize_session=False)
    )
    db.commit()
    return int(n or 0)


def claim_this_one(db, m) -> Optional[Claim]:
    """The claim of an inline extraction (a synchronous write): the same
    claim as the drain's, on ONE memory, as a compare-and-set from 'pending'
    (not forgotten, the text as read now) to 'processing', stamped now.
    Commits. None when the memory is not free: a drain or another request
    holds it, it was forgotten, or it changed between the read and the claim.

    Until 2.11.0 an inline extraction worked on a 'pending' memory without
    claiming it, so a drain could claim and extract it at the same time (two
    model calls billed, two passes of the judge; an audit of 2.10.0),
    and nothing told either one that the memory changed under it."""
    M = type(m)
    text = db.query(M.content).filter(M.id == m.id).scalar()
    now = datetime.now(timezone.utc)
    n = (
        db.query(M)
        .filter(M.id == m.id, M.graph_sync_status == "pending",
                M.content.is_(None) if text is None else M.content == text,
                _not_forgotten(M))
        .update({M.graph_sync_status: "processing", M.graph_sync_claimed_at: now}, synchronize_session=False)
    )
    db.commit()
    if not n:
        return None
    return Claim(M, m.id, now, text)


def inline(db, m, do: Callable[[Claim], Any]) -> Any:
    """An inline extraction under a claim (2.11.0): `claim_this_one`, then
    `do(claim)`, which extracts `claim.text` and writes with
    `write_memory(..., claim=claim)`; a claim `do` did not close (a
    failure, a pause) goes back to the queue ('pending', as an inline write
    that failed always left it, for the drain). Returns what `do` returns,
    None when the memory was not free (somebody else extracts it)."""
    p = claim_this_one(db, m)
    if p is None:
        logger.info("[memory] memory %s is not free for an inline extraction (claimed, forgotten "
                    "or changed): left to whoever holds it", m.id)
        return None
    try:
        return do(p)
    except Exception:
        db.rollback()
        raise
    finally:
        try:
            release(db, p)
        except Exception as e:  # noqa: BLE001 - the reaper takes back what this leaves
            # 3.0.0: never `exc_info`, whose formatter prints the
            # message of a database error, row values included (`log_safe`).
            logger.error("[memory] could not give memory %s back to the queue: %s\n%s", m.id,
                         _log.error(e), _log.stack(e))
            db.rollback()


class _ClaimLost(RuntimeError):
    """The memory changed under its write (`still_its_own` no longer holds)."""


def by_group(rows: Sequence, key: Callable[[Any], Any]) -> Dict[Any, list]:
    """Memories grouped by owner and end user: the judge's candidates never
    cross an end user, so two groups are independent."""
    groups: Dict[Any, list] = defaultdict(list)
    for m in rows:
        groups[key(m)].append(m)
    return groups


def _new_for_the_batch(
    written: Sequence[Tuple[Any, list]], memory_id: Any, extracted: Sequence = (),
    judged: frozenset = frozenset(),
) -> List[dict]:
    """The facts of this write that the batched judge must weigh: born from
    this memory, current, open, not a reconfirmation of an existing fact
    (the per-fact path returns early on those too), once each. `extracted`:
    the extracted facts `written` was written from, in the same order; their
    words reach the judge (2.5.0). `judged` (6.19.0): rows the write already
    judged as present facts (`store.judged_by_the_write`), left out."""
    seen = set()
    new_facts = []
    for i, (fact, _) in enumerate(written):
        if fact.source_memory_id != memory_id or fact.id in seen or fact.id in judged:
            continue
        if (fact.tense or "current") != "current" or fact.invalid_at is not None:
            continue  # history and plans do not close anything
        if (fact.observation_count or 1) > 1:
            continue  # a reconfirmation of an existing fact, not a new one
        words = getattr(extracted[i], "evidence", None) if i < len(extracted) else None
        seen.add(fact.id)
        new_facts.append({
            "id": fact.id,
            "end_user_id": fact.end_user_id,
            "subject": fact.subject_label,
            "subject_canonical": fact.subject_canonical,
            "predicate_raw": fact.predicate_raw,
            "predicate_canonical": fact.predicate_canonical,
            "object": fact.object_label,
            "object_canonical": fact.object_canonical,
            "embedding": fact.embedding,
            "valid_from": fact.valid_from,
            "source_memory_id": memory_id,
            "evidence": words,
        })
    return new_facts


def _gone(db, M: Any, mid: Any) -> bool:
    """True when the memory's row is no longer in the database. Read from the
    database, never from the class or the text of the error: an erased memory
    fails a write as a stale UPDATE (its row was changed) or as a foreign-key
    violation (a fact inserted for it), and nothing is left to write either way."""
    try:
        return db.query(M.id).filter(M.id == mid).first() is None
    except Exception:  # noqa: BLE001 - not knowing is not "erased": the error stays
        db.rollback()
        return False


def write_memory(
    db,
    memory: Any,
    extracted: Sequence,
    *,
    write_one: Callable[[Any, bool], Tuple[Any, list]],
    shape_of: Callable[[Any, list], dict],
    batch: Optional[Callable[[List[dict]], Dict[Any, list]]] = None,
    retire: Optional[Callable[[set], Any]] = None,
    claim: Optional[Claim] = None,
    objects: Optional[Callable[[Sequence], Any]] = None,
    shared: Optional[Callable[[set], Any]] = None,
) -> List[dict]:
    """Write the facts extracted from ONE memory, mark it 'done', commit.
    Returns the write shapes (`shape_of(fact, closed_ids)`).

    `write_one(ef, defer)`: the engine's write of one fact
    (`writing.write_fact`). `batch(new_facts)`: when given, every fact is
    written with the judge deferred and the whole write is judged in one call
    (`judge.batch`); otherwise each fact is judged as it is written.

    One transaction: a model the write needs that gives no usable answer (the
    judge with no verdict, the vocabulary that does not answer) rolls back the
    whole memory, facts and closings, and returns []: the memory stays as it
    was ('pending' on a synchronous write, 'processing' in the drain, which
    `drain_one` turns into 'error'). Until 2026-09-28 a failed judge kept the
    new fact open next to the one it may contradict and marked the memory done.

    `retire(kept)` (2.3.0): called with the ids of the facts this round wrote
    or restated, in the same transaction, so the engine can close what an
    earlier version of the memory stated and this one no longer does
    (`store.retire_the_previous_version`). An edited memory used to keep
    its old facts open next to the new ones: the rule that a memory's facts
    never close each other (BUG-123) held across two versions of its text.

    `claim` (2.11.0): the claim the facts were extracted under (`claim_pending`
    + `claim`, or `claim_this_one`). 'done' is then written only if the row is
    still that claim's (`still_its_own`), in the write's transaction; if it is
    not (forgotten, edited, taken again), the whole write rolls back, facts,
    closings and the earlier version's retirement, and [] comes back with
    `claim.outcome == "lost"`. A memory edited meanwhile stays 'pending' with
    its new text, for the next extraction. Without `claim`, 'done' is written
    as before 2.11.0, unchecked (for an engine that has not adopted it).

    `objects(extracted)` (6.1.0, off unless given): called first, in the same
    transaction, so that a new object the writer says is the same thing as
    one the memory holds is linked to it (`objects.unite_new_objects`): a
    link row only, the facts are written exactly as without it, and a failed
    question rolls the write back like every other.

    `shared(kept)` (6.2.0, for an engine that passes it, with `retire` and
    `batch`): called once the facts are written, with the ids this round
    wrote or restated, in the same transaction; it returns the ids of the
    copies the memory's previous text shared with other memories and the
    new one does not state (`store.shared_by_the_previous_version`). The
    batched judgement weighs them as another memory's facts
    (`store.judged_apart`), and `retire` takes the memory out of their
    sources. Ignored without `batch`: a judgement made while the text is
    being written cannot know yet which copies it states again. Without it,
    an edit is judged as before 6.2.0, never against a fact its old text
    shared."""
    out: List[dict] = []
    try:
        # 3.1.3: the closings of this write, so every judgement in it weighs
        # the facts as they were before it (`store.one_write`).
        with _store.one_write(db):
            if objects is not None and extracted:
                objects(extracted)
            written = [write_one(ef, batch is not None) for ef in extracted]
            _point_to_the_successor([fact for fact, _ in written], db)
            apart = set(shared({fact.id for fact, _ in written}) or ()) \
                if shared is not None and batch is not None else set()
            with _store.judged_apart(db, memory.id, apart):
                closed = batch(_new_for_the_batch(written, memory.id, extracted,
                                                  _store.judged_by_the_write(db))) if batch is not None else {}
        if batch is not None:
            for fact, own in written:
                # 2.12.0: what the write of the fact closed on its own (a past
                # statement, a plan come true, judged one by one even in a
                # batched write) joins what the batch closed. It was dropped
                # from the answer and from the webhooks, closed all the same.
                everything = list(closed.get(fact.id, []))
                everything += [i for i in (own or []) if i not in everything]
                out.append(shape_of(fact, everything))
        else:
            for fact, invalidated in written:
                out.append(shape_of(fact, invalidated))
        if retire is not None:
            retire({fact.id for fact, _ in written})
        if claim is None:
            memory.graph_sync_status = "done"
        elif not close_the_claim(db, claim, "done"):
            raise _ClaimLost(str(memory.id))
        db.commit()
    except _ClaimLost:
        # The key before the rollback, from the identity map: an erased memory's
        # row cannot be loaded again (5.12.3).
        mid = inspect(memory).identity[0]
        db.rollback()
        claim.outcome = "lost"
        logger.info("[memory] memory %s changed while its facts were extracted (edited, forgotten or "
                    "claimed again): this write is discarded", mid)
        return []
    except Exception as e:
        # An audit of 2.10.0: with the stack, or the log says
        # what failed and never where. 3.0.0 (code audit of 6 Oct):
        # without the message. A database error's message carries the
        # statement's parameters and PostgreSQL's DETAIL, the values of the
        # row (a fact's subject and object), and `exc_info` prints it: the
        # classes, SQLSTATE and constraint (`log_safe.error`) and the frames
        # (`log_safe.stack`) say what failed and where.
        # The key from the identity map: a failed flush has expired the object,
        # and reading `memory.id` would load a row that may no longer exist.
        mid, M = inspect(memory).identity[0], type(memory)
        db.rollback()
        if _gone(db, M, mid):
            # 5.12.2, 5.12.3 (1.0 fixes, 27 Oct 2026): the erasure of a memory while its facts
            # are extracted is an expected race, read from the database (the row
            # is gone), never from the error's text. The write is abandoned as
            # before, not queued again: nothing is left to extract.
            logger.info("[memory] memory %s was erased while its facts were extracted: "
                        "this write is discarded", mid)
            return []
        logger.log(logging.ERROR if any(isinstance(x, StaleDataError) for x in _log._chain(e)) else logging.WARNING,
                   "[memory] persist failed for memory %s, nothing written: %s\n%s", mid,
                   _log.error(e), _log.stack(e))
        return []
    return out


def _point_to_the_successor(facts: Sequence[Any], db: Any = None) -> None:
    """6.2.1: a fact a past statement closed (in place, or born closed) points
    to the fact of the same write that replaced it: the one open fact about
    the same subject in the same relation, with another value. The judge's
    closing sets `invalidated_by_id`; a past statement closes without the
    judge, so the pointer stayed empty and the history had a hole (E3 of the
    engine list, 4 Nov 2026). An invalidated edge keeps the edge that
    invalidated it (Graphiti, Rasmussen et al. 2025, section 2.2.3). Only a
    single successor is taken: with two candidates nothing says which one,
    and the pointer stays empty. Nothing is judged and nothing else closes.
    6.14.0: a row the write held over (`store.mark_held`, a fact the
    caller's word keeps over) is not one of the write's: it is left as it
    was, never pointed, never a successor."""
    held = _store.held_by_the_write(db) if db is not None else frozenset()
    rows = [f for f in facts if f is not None and f.id not in held]
    for past in rows:
        if getattr(past, "tense", None) != "past" or past.invalid_at is None or past.invalidated_by_id is not None:
            continue
        successors = {f.id: f for f in rows
                      if f.id != past.id and f.invalid_at is None
                      and f.subject_canonical == past.subject_canonical
                      and f.predicate_canonical == past.predicate_canonical
                      and f.object_canonical != past.object_canonical}
        if len(successors) == 1:
            past.invalidated_by_id = next(iter(successors))


def drain_one(db, m: Any, do: Callable[[Any, Optional[datetime]], Any], *, what: str = "pending process") -> bool:
    """One claimed memory: `do(m, event_time)` extracts and writes it (or
    writes facts already extracted). A memory still 'processing' afterwards
    failed inside (the write rolled back): it goes to 'error', counted and
    re-drivable, never looped forever. False when this call filed it 'error';
    True otherwise: written, or no longer this drain's.

    2.11.0: the claim is read first (`claim`), and a memory that is no longer
    one (edited back to 'pending', forgotten, closed meanwhile) is left to
    whoever has it now, with no call. 'error' is a compare-and-set on that
    claim (`close_the_claim`): until then it was written on the session's
    object, so a memory edited during the extraction and taken by a second
    drain was filed 'error' under that drain's claim, and one forgotten
    meanwhile lost its forgetting. The engine's `do` reads its own token
    with `claim(db, m)` and writes with `write_memory(..., claim=)`.

    5.2.3: after `do` only the claim is read, never the session's object.
    The row may be gone (an account closed or a DELETE /v1/agents/{id} during
    the extraction) or the session left needing a rollback, and reading
    `m.graph_sync_status`, or `m.id` for the log, reloaded the row:
    ObjectDeletedError or PendingRollbackError came out of the drain and the
    rest of its group stayed 'processing' until the reaper (a code review,
    and the account closings of 0.1.38 and 0.1.39). The
    compare-and-set already answers "still this drain's and 'processing'",
    and a failure of a memory that is no longer this drain's is the end of
    that claim, logged as such: a warning only when this call files it."""
    p = claim(db, m)
    if p is None:
        logger.info("[memory] memory %s is no longer claimed (edited, forgotten or closed meanwhile): skipped",
                    m.id)
        return True
    try:
        do(m, event_time_of(m))
        # What `do` left in the session first (a status it set on the object),
        # then the claim: the bulk UPDATE does not flush it.
        db.flush()
        if close_the_claim(db, p, "error"):
            db.commit()
            return False
        return True
    except Exception as e:
        try:
            db.rollback()
            filed = close_the_claim(db, p, "error")
            db.commit()
        except Exception as e2:
            logger.warning("[memory] async %s failed for %s: %s\n%s", what, p.id, _log.error(e), _log.stack(e))
            logger.error("[memory] could not file memory %s as 'error': %s\n%s", p.id,
                         _log.error(e2), _log.stack(e2))
            db.rollback()
            return False
        if filed:
            # 2.11.0: with the stack; 3.0.0: without the message.
            logger.warning("[memory] async %s failed for %s: %s\n%s", what, p.id, _log.error(e), _log.stack(e))
            return False
        logger.info("[memory] async %s for %s ended with %s: the memory is no longer this drain's "
                    "(edited, forgotten, erased or taken meanwhile)", what, p.id, _log.error(e))
        return True
