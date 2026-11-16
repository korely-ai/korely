"""What a memory written again does to the facts its previous text stated:
the two hooks of memory_core `worker.write_memory`, `shared` once the new
text's facts are written and `retire` after its judgement
(service._persist_extracted).

An edited memory is extracted again, and its facts never closed each other
(BUG-123), so the previous version's stayed open next to the new ones:
"prefers email" edited to "now prefers phone" left four facts true (the
hosted product's audit, 2026-09-29). What this round did not write or
restate is closed at the arrival of the new text, in the same transaction; a
fact another memory still states only loses this one.

memory_core 6.2.0: the copies the previous text stated with other memories
and the new one does not state are weighed by the write's judgement as
another memory's facts, and the memory then leaves their sources. "I work at
Acme" in three memories, one edited to "now at Beta": Acme was never shown
to the judge and stayed open beside Beta (the copies,
tests/test_copies_are_closed_together.py).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, List, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from ...db.models import AgentFact
from ..memory_write import SESSION
from .entities import TABLES, memory_scope
from .erasure import after_the_retirement, stated_by_the_previous_version
from .temporal import not_ended


def hooks(db: Session, tenant_id: UUID, memory: Any, arrived: Optional[datetime], *,
          emit_retired: Callable[[Session, UUID, List[Any], UUID], None],
          ) -> Tuple[Callable[[set], set], Callable[[set], None], Callable[[Any, Any], Any]]:
    """(shared, retire, said) for one write of `memory`, whose new text
    arrived at `arrived` (`worker.arrival`). `emit_retired` announces the
    facts the edit retired (service._emit_retired). `said(ef, written)`
    takes each statement of the write and what memory_core wrote it on
    (`write_one`), and returns it as it came: a past statement says the row
    it lands on, when that row was there, and what its judgement closed are
    over, and this memory ended them in place (given_back.said_over).

    The memory's word on a row it ended in place lasts as long as its text
    says it: what an earlier version ended and this one no longer says is
    over (no past statement of it lands on the row) is taken back by
    `retire`, as the facts the new text no longer states are, and given
    back where nothing else closes it; and what the edit retires, it ended in
    place (`agent_fact_ended_by`), so a deletion of the value an earlier
    judge named for the fact does not give it back while the memory stands.
    A fact a past statement's judgement closed is said as long as the row
    that statement lands on is (`given_back.said_over`)."""
    from ..memory_core.store import retire_the_previous_version, shared_by_the_previous_version
    from .given_back import ended_by_now, ended_in_place_by, no_longer_said, said_over, \
        standing_corrections

    # Within the memory's scope (memory_core 2.12.0): a fact is born and
    # reconfirmed inside it, and without it every write read the whole
    # facts table, every customer's (41 ms against 1 ms on 121,379 facts).
    # "Not ended yet", not "true now", as `forget_memory` asks and as
    # memory_core 2.11.0 says to pass: a fact of the previous text dated
    # ahead (a date in it up to a day ahead) was not true yet, so it was
    # left open and came true at its date although the new text no longer
    # said it (audit of 17 Oct 2026). It ends at its own start.
    scope = memory_scope(memory.tenant_id, memory.project_id, memory.end_user_id)
    apart: set = set()
    landed: set = set()
    # 1.0.27 (the decision of 15 Nov 2026: a correction is the person's will,
    # as a forget is): a correction by hand of one of this memory's facts
    # keeps the memory as its source, and the new text leaves it standing,
    # whatever it says (given_back.standing_corrections). Read before the
    # write, as the rest of what the edit weighs.
    corrections = standing_corrections(db, memory, scope)

    def said(ef: Any, written: Any) -> Any:
        said_over(db, memory, written, getattr(ef, "tense", None), landed)
        return written

    def shared(kept: set) -> set:
        kept = kept | corrections
        apart.clear()
        apart.update(shared_by_the_previous_version(
            db, TABLES, memory.id, kept, is_open=not_ended(), scope=scope))
        return apart

    def retire(kept: set) -> None:
        kept = kept | corrections
        # A note's previous version may leave a fact only sessions state
        # (AgentFact.session_only): the facts it stated and no longer does,
        # read before the engine detaches it, are counted again after (audit
        # of 19 Oct 2026), the copies the judge weighed included, open or
        # closed by it. A session detached changes nothing.
        detached = [] if memory.kind == SESSION else stated_by_the_previous_version(
            db, memory.id, kept | apart, scope) + copies(apart - kept)
        retired = retire_the_previous_version(
            db, TABLES, memory.id, kept, when=arrived, is_open=not_ended(), scope=scope,
            apart=sorted(apart, key=str))
        # The rows an earlier version ended in place and this one no longer
        # says are over (none of its past statements lands on them, nor on
        # the row they follow): its word on them is taken back, from this
        # text's arrival. What the edit retired, it ended in place.
        unsaid = [f for f in ended_in_place_by(db, memory.tenant_id, [memory.id],
                                               lambda q: q.filter(scope))
                  if f.id not in landed and f.invalidated_by_id not in landed]
        no_longer_said(db, memory, unsaid)
        ended_by_now(db, memory, retired)
        # What the edit took away: `session_only` counted again, and what the
        # retired facts and the words taken back had closed given back
        # (erasure.after_the_retirement).
        after_the_retirement(db, tenant_id, detached, retired, scope, ended=unsaid, until=arrived,
                             by=[memory.id])
        if retired:
            emit_retired(db, tenant_id, retired, memory.id)

    def copies(ids: set) -> List[Any]:
        return db.query(AgentFact).filter(AgentFact.id.in_(list(ids))).all() if ids else []

    return shared, retire, said
