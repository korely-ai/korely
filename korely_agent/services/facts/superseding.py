"""A fact closed by the fact that takes its place, said by its caller: a
correction (`correct_fact`, PATCH /v1/facts/{id}) or a direct write's
`replaces` (POST /v1/facts). The checks before anything is written
(`fact_it_replaces`), the closing at the write, under the lock of both rows
(`close_superseded`), and the answers a write that met a change made
meanwhile gives (FactNotCurrent, RestatedFactClosed, `current_successor`).
Moved out of services/facts/service.py on 26 Nov 2026, as it was there.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from ...core.times import aware
from ...db.models import AgentFact
from ..ids import fact_public_id
from ..memory_core.fact_time import effective_tense
from .erasure import ClosedBeforeItBegan
from .given_back import record_by_hand
from .reads import load_fact
from .temporal import true_now


class ReplacesAnotherMemory(ValueError):
    """`replaces` names a fact a direct write may not take the place of:
    another end user's or agent's memory, or a new fact that is not a
    current one (a past or a planned statement ends nothing now). 422."""


def fact_it_replaces(db: Session, tenant_id: UUID, replaces: UUID, *, project_id: Any,
              end_user_id: Optional[str], agent_id: Optional[str], tense: str,
              valid_from: Optional[datetime],
              valid_from_is_a_day: bool = False) -> Optional[AgentFact]:
    """The fact `replaces` names, when the write may close it; None when it
    is not one of this project's. The checks of `write_fact_direct`, before
    anything is written."""
    old = load_fact(db, tenant_id, replaces, project_id=project_id)
    if old is None:
        return None
    if (old.end_user_id, old.agent_id) != (end_user_id, agent_id):
        raise ReplacesAnotherMemory(
            f"{fact_public_id(old.id)} is the memory of another end user or agent: a fact "
            f"replaces only one with the same user_id and agent_id.")
    if effective_tense(tense) != "current":
        raise ReplacesAnotherMemory(
            "replaces takes a current fact: a past or a planned one ends nothing now. "
            "To end a fact at a date, use POST /v1/facts/{fact_id}/forget with `at`.")
    now = datetime.now(timezone.utc)
    if not true_now(old, now):
        raise FactNotCurrent(fact_public_id(old.id), current_successor(db, old, now))
    begins = aware(valid_from) if valid_from is not None else now
    if old.valid_from is not None and begins < aware(old.valid_from) and not (
            valid_from_is_a_day and _same_utc_day(begins, aware(old.valid_from))):
        # A start given as a day and no time is the same valid time as any
        # start of that UTC day (as contradiction._close_from_a_day): the
        # fact it replaces is closed at its own start (`write_fact_direct`, never before it began).
        raise ClosedBeforeItBegan(fact_public_id(old.id), aware(old.valid_from), begins)
    return old


def _same_utc_day(a: datetime, b: datetime) -> bool:
    return a.astimezone(timezone.utc).date() == b.astimezone(timezone.utc).date()


def as_it_was_read(fact: AgentFact) -> tuple:
    """What `close_superseded` compares when it closes `fact`: its end and what
    ended it, as the write read them before its model calls."""
    return (fact.invalid_at, fact.invalidated_by_id)


def close_superseded(db: Session, tenant_id: UUID, old: AgentFact, new_fact: AgentFact,
                begins: datetime, closed_by_contradiction: List[UUID], *,
                as_read: tuple) -> Optional[List[UUID]]:
    """Close `old` where `new_fact` begins, `invalidated_by` it, in the
    caller's transaction, and the ids the write superseded: `old` first,
    then whatever else the new fact's contradiction pass closed. For a
    correction (`correct_fact`) and a fact posted with `replaces`.

    `old` is locked here, at its closing, and not before the write's model
    calls, then read again and compared with what the write read first
    (`as_it_was_read`, `as_read`): changed by another transaction since (closed,
    or its end moved), the write is rolled back and FactNotCurrent raised,
    so of two writes closing one fact at once the second answers 409.
    Locked when it was read (d0636f64), the row stayed held through the
    judge, and a forget or a worker waited up to their 5 s for it (review of
    fix/limiti-dns-replaces, 26 Nov 2026): an optimistic check, the version
    compared at the write (Kung and Robinson, "On optimistic methods for
    concurrency control", ACM TODS 1981). A change this write made itself
    (its judge closing `old` for the new fact) is its own. The fact the
    write lands on is locked with it, in the order of their ids, and must
    be as this write left it (RestatedFactClosed). None, rolled back, when either row
    was deleted meanwhile.

    An end still ahead is brought to `begins`, the comparison forget_fact
    makes: until 18 Oct 2026 only an open fact was closed, so a value set to
    end in 2030 and corrected today stayed true next to its correction until
    2030, while the answer and its `fact.invalidated` said it was closed
    (audit of 18 Oct 2026). `old` counts when it now points at the new one:
    a fact a different fact had already superseded is history this call did
    not change, and announcing it again would be a second, false event."""
    # The fact the write lands on as this write left it, its own judge's
    # closing included (a new fact judged history ends where it began).
    landed = as_it_was_read(new_fact)
    db.flush()
    # Both rows, in the order of their ids: the fact closed and the one the
    # write lands on, which is a row of before when the write restated a
    # fact already true (memory_core reconfirms it). Two writes swapping
    # values at once each closed the fact the other restated, both answered
    # 201 and no value was left true (the review's P8b, 26 Nov 2026).
    held = {f.id: f for f in (db.query(AgentFact)
                              .filter(AgentFact.id.in_(sorted({old.id, new_fact.id})))
                              .order_by(AgentFact.id)
                              .with_for_update().populate_existing().all())}
    if old.id not in held or new_fact.id not in held:
        # Deleted meanwhile (an erasure of its memory or its end user): there
        # is nothing left to close, nor to answer with (the caller's 404).
        db.rollback()
        return None
    now = datetime.now(timezone.utc)
    if as_it_was_read(old) != as_read and old.invalidated_by_id != new_fact.id:
        successor = current_successor(db, old, now)
        db.rollback()
        raise FactNotCurrent(fact_public_id(old.id), successor)
    if as_it_was_read(new_fact) != landed:
        # The fact this write lands on was changed by another write
        # meanwhile (closed, or its end moved). Compared with what this write
        # left, not with "open": a fact with an end still ahead is true now
        # and memory_core reconfirms it, and this write's own judge may close
        # the new fact as history (the review's P11 and P12, 26 Nov 2026).
        successor = current_successor(db, new_fact, now)
        db.rollback()
        raise RestatedFactClosed(fact_public_id(new_fact.id), successor)
    if old.invalid_at is None or aware(old.invalid_at) > begins:
        old.invalid_at = begins
        old.invalidated_by_id = new_fact.id
    elif old.invalidated_by_id is None:
        old.invalidated_by_id = new_fact.id
    record_by_hand(db, tenant_id, old, invalidated=1)
    return ([old.id] if old.invalidated_by_id == new_fact.id else []) + [
        i for i in closed_by_contradiction if i != old.id]


class FactNotCurrent(ValueError):
    """A correction asked of a fact that is history: superseded, forgotten or
    ended. Corrected anyway, the new value was written and the contradiction
    pass on it closed the fact that holds now, so correcting the past changed
    the present (a new-customer test of 7 Oct 2026, the hosted
    product's 409 `fact_not_current`). `current_id` is the fact that superseded
    it, followed to the one true now, or None when nothing did."""

    def __init__(self, fact_id, current_id):
        super().__init__(f"fact {fact_id} is no longer current")
        self.fact_id, self.current_id = fact_id, current_id


class RestatedFactClosed(FactNotCurrent):
    """The fact a write lands on (a fact already true it restates) was
    changed by another write while this one ran (closed, or its end moved):
    nothing is written, 409 `fact_not_current` as for the fact it closes, in
    words of its own."""


def current_successor(db: Session, fact: AgentFact, now: datetime) -> Optional[UUID]:
    """The fact that holds now, down the chain of `invalidated_by` from one that
    is history; None when the chain ends without one (forgotten, ended, or a
    successor itself closed with nothing after it). However long the chain:
    a bound of 50 links answered None for a fact updated more often (review
    of piece 2 of agent notices, 8 Nov 2026); a loop, a corrupt store, ends
    the walk at the first fact seen again."""
    seen, nxt = {fact.id}, fact.invalidated_by_id
    while nxt is not None and nxt not in seen:
        seen.add(nxt)
        f = db.get(AgentFact, nxt)
        if f is None or f.tenant_id != fact.tenant_id:
            return None
        if true_now(f, now):
            return f.id
        nxt = f.invalidated_by_id
    return None
