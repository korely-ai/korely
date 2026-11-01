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
          ) -> Tuple[Callable[[], None], Callable[[set], None]]:
    """(shared, retire) for one write of `memory`, whose new text arrived
    at `arrived` (`worker.arrival`). `emit_retired` announces the facts the
    edit retired (service._emit_retired)."""
    from ..memory_core.store import retire_the_previous_version, shared_by_the_previous_version

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

    def shared(kept: set) -> set:
        apart.clear()
        apart.update(shared_by_the_previous_version(
            db, TABLES, memory.id, kept, is_open=not_ended(), scope=scope))
        return apart

    def retire(kept: set) -> None:
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
        # What the edit took away: `session_only` counted again, and what the
        # retired facts had closed given back (erasure.after_the_retirement).
        after_the_retirement(db, tenant_id, detached, retired, scope)
        if retired:
            emit_retired(db, tenant_id, retired, memory.id)

    def copies(ids: set) -> List[Any]:
        return db.query(AgentFact).filter(AgentFact.id.in_(list(ids))).all() if ids else []

    return shared, retire
