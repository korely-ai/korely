"""What a row's vector job is: its states, its ladder of retries, and the
three changes a write makes to it (services/vectors.py tells the whole
story, and runs the drain).

A module of its own, below everything (audit of 17 Oct 2026): the facts'
write and the memories' write set these columns, and the drain reads them
and calls both writers and the facts' judge. With the two halves in one
file, the writers imported the drain and the drain imported the writers, a
circle held only by imports inside functions: `from .facts.service import
fact_vector` moved to the top of vectors.py stopped `import
korely_agent.services.facts`, and the service with it. Now the writers
import this, which imports nothing of theirs, and the drain imports them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from ..db.models import AgentFact, AgentMemory

PENDING, DONE, ERROR = "pending", "done", "error"
#: A vector from the embedding model before this one (`vectors.recompute`).
STALE = "stale"

#: Calls per text, the one on the write included, before the row is `error`.
ATTEMPTS = 12
#: The wait after the first failed call; each next wait doubles it...
FIRST_PAUSE = timedelta(minutes=1)
#: ...up to this.
LONGEST_PAUSE = timedelta(hours=6)
#: Failed calls in a row after which a pass stops: the provider is down, and
#: every further row would spend one of its attempts (and, on a provider that
#: hangs, a whole timeout) learning the same thing. The rows not reached keep
#: their attempts and wait for the next pass.
FAILURES_BEFORE_STOPPING = 3


def pause_after(attempts: int) -> timedelta:
    """How long to wait after the `attempts`-th failed call."""
    n = max(1, int(attempts or 1))
    # Doubling in a loop rather than 2**n: 2**n of a large count is a very
    # large timedelta for no reason, and the cap is reached at the tenth.
    wait = FIRST_PAUSE
    for _ in range(n - 1):
        wait *= 2
        if wait >= LONGEST_PAUSE:
            return LONGEST_PAUSE
    return min(wait, LONGEST_PAUSE)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def cleared(row) -> None:
    """The text changed, or is new: no vector until one is computed for it,
    and a fresh count of attempts. Does not commit."""
    row.embedding = None
    row.embedding_status = PENDING
    row.embedding_attempts = 0
    row.embedding_next_at = None


def renamed(row) -> None:
    """A fact's canonical names changed (a name declared or removed): its
    vector is computed again on the new text, but the fact was judged when it
    was written, so the drain asks no judge for it (`judged_before_vector`).
    Not `cleared`: that one is a vector that failed on its write, and the
    late judgement is for those. Does not commit."""
    # Judged already if the flag says so, or if it had a vector (a vector
    # failed on the write means the write could not judge it, and a rename
    # must not take that judgement away).
    judged = bool(row.judged_before_vector) or row.embedding_status in (DONE, STALE)  # had one; the vector itself is not read
    cleared(row)
    row.judged_before_vector = judged


def missed(row, *, now: Optional[datetime] = None) -> None:
    """The call on the write gave no vector: the row waits, with that call
    counted as the first attempt. Does not commit; the write goes on."""
    row.embedding = None
    row.embedding_status = PENDING
    row.embedding_attempts = 1
    row.embedding_next_at = (now or utc_now()) + pause_after(1)
    if hasattr(row, "judged_before_vector"):
        row.judged_before_vector = False  # its write could not judge: the drain does


def requeue_given_up(db: Session, *, limit: int = 0) -> tuple[int, int]:
    """`manage reextract`: the rows whose vector gave up, waiting again with
    a fresh count. Returns (memories, facts). Does not commit."""
    fresh = {"embedding_status": PENDING, "embedding_attempts": 0, "embedding_next_at": None}
    counts = []
    for model, live in ((AgentMemory, (AgentMemory.deleted_at.is_(None),)), (AgentFact, ())):
        q = db.query(model.id).filter(model.embedding_status == ERROR, *live)
        if limit:
            q = q.limit(limit)
        ids = [r[0] for r in q.all()]
        n = 0
        if ids:
            n = (db.query(model).filter(model.id.in_(ids))
                 .update(dict(fresh), synchronize_session=False))
        counts.append(int(n or 0))
    return counts[0], counts[1]
