"""What a transaction closed, read from the rows it closed, and one
`fact.invalidated` for each.

The events used to come from what each write SAID it closed: the
`invalidated` of its write shape (`service.emit_invalidations`), the facts an
edit retired, the fact a forget closed. A closure the write did not name went
without one. The functional test of 1.0.0 (28 Oct 2026) found it: "Luca
works at Acme", then "Luca left Acme and joined Globex", and the past
statement closes "works at Acme" in place (memory_core
`writing._the_past`: the memory held it true, the text says it is over,
that row is closed instead of adding a closed twin). The write's fact is
that closed row, so it is in nobody's `invalidated`, and the store closed a
fact that subscribers were never told about.

So the closures are watched where they happen, on the rows: a fact the
store held open that has its `invalid_at` set or brought earlier, in this
session's transaction, by whichever code (the engine's past statement, its
plan come true, the judge, a correction, a forget), is recorded on the
session (`_closing`). Every door that writes a fact announces, before its
commit, what it closed: first what its write shape names, with the fact
that closed it, then every other fact the transaction closed, with the
`invalidated_by` the row carries (None for a closure in place). Each fact is
announced once per transaction (`announce`), whichever path gets to it first,
and by the pass only when the committed row was still open (a race's loser
closes again what the winner closed and announced: `_open_when_committed`).

Not a closure: a row born closed (a past statement with nothing open to
close, a history row), which nobody held true; nor a fact born in the same
transaction, never seen open outside it. The record ends with its
transaction, committed or rolled back (`_ended`): a closure a rollback
undid is not announced, and what one transaction closed is not announced by
the next. The doors that announce are the ones that did before: a memory's
write and its edit, POST /v1/facts, a correction, a fact's forget and a
late vector's judgement (`service.emit_invalidations`, `_emit_retired`,
`forget_fact`). A memory forgotten or an end user erased closes facts and
announces nothing, as before, and what it closes is marked `unsaid`, so a
write later in the same transaction does not announce it either; the engine's closures on those doors are ORM
writes, which the `set` event sees, never bulk UPDATEs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session, object_session
from sqlalchemy.orm.base import NO_VALUE, NEVER_SET

from ...core.times import aware
from ...db.models import AgentFact
from ..ids import fact_public_id

# On `Session.info`, for the transaction in progress.
_CLOSED = "korely.facts.closed"        # {fact id: the row}, in the order closed
_BORN = "korely.facts.born"            # {fact id} inserted by this transaction
_ANNOUNCED = "korely.facts.announced"  # {public fact id} with its event enqueued
_UNSAID = "korely.facts.unsaid"        # {fact id} a forget or an erasure closed


def _closing(target: AgentFact, value, oldvalue, _initiator) -> None:
    """A fact's end set, or brought earlier: a closure, when the row is
    already in the database. A later end, or one cleared, closes nothing."""
    if value is None:
        return
    if oldvalue not in (None, NO_VALUE, NEVER_SET) and aware(oldvalue) <= aware(value):
        return
    db = object_session(target)
    if db is None or not inspect(target).persistent:
        return
    db.info.setdefault(_CLOSED, {})[target.id] = target


def _born(db: Session, instance) -> None:
    if isinstance(instance, AgentFact):
        db.info.setdefault(_BORN, set()).add(instance.id)


def _ended(db: Session, transaction) -> None:
    # The outer transaction only: a savepoint's end is part of it (a closure
    # a savepoint's rollback undid is read again from its row, `_still_closed`).
    if transaction.parent is None:
        for k in (_CLOSED, _BORN, _ANNOUNCED, _UNSAID):
            db.info.pop(k, None)


def born_here(db: Session, fact) -> bool:
    """`fact` (a row or its id) was inserted by this session's transaction
    (`_born`)."""
    return getattr(fact, "id", fact) in (db.info.get(_BORN) or set())


def closed_here(db: Session, fact: AgentFact) -> bool:
    """`fact`, in the database before this transaction, had its end set or
    brought earlier by it (`_closing`): a closure this transaction made, by
    whichever code (facts/service.write_fact_direct reads it for a past
    statement posted by hand)."""
    return fact.id in (db.info.get(_CLOSED) or {}) and not born_here(db, fact)


def unsaid(db: Session, facts) -> None:
    """Facts a forget or an erasure closes (erasure._invalidate_facts): never
    announced by the pass, whatever else the transaction then writes. Those
    doors announce nothing, and a later write in the same transaction
    announced them for them (review of fix/1.0-screen, 28 Oct 2026)."""
    db.info.setdefault(_UNSAID, set()).update(f.id for f in facts)


# Registered once, when the facts service is imported: on the attribute, and
# on every Session (the request's, the workers', an own session).
event.listen(AgentFact.invalid_at, "set", _closing, active_history=True)
event.listen(Session, "pending_to_persistent", _born)
event.listen(Session, "after_transaction_end", _ended)


def _still_closed(f: AgentFact) -> bool:
    try:
        return f.invalid_at is not None
    except Exception:  # noqa: BLE001 - a row gone with a rollback closed nothing
        return False


def closed_unannounced(db: Session, tenant_id: UUID) -> List[AgentFact]:
    """The facts of `tenant_id` this transaction closed that have no event
    yet, in the order they were closed. The row is read only once it is known
    to be there (`_still_closed`): one deleted after its closure raised
    ObjectDeletedError here and rolled the write back (review, 28 Oct)."""
    skip = (db.info.get(_BORN) or set()) | (db.info.get(_UNSAID) or set())
    said = db.info.get(_ANNOUNCED) or set()
    mine = [f for i, f in (db.info.get(_CLOSED) or {}).items()
            if i not in skip and fact_public_id(i) not in said
            and _still_closed(f) and f.tenant_id == tenant_id]
    return _open_when_committed(mine)


def _open_when_committed(facts: List[AgentFact]) -> List[AgentFact]:
    """The facts whose committed row is still open at the end this
    transaction gave them: the closures that are this transaction's.

    Two writes that complete the same plan at once (memory_core
    `writing._insert`, the loser's branch): the winner closes the plan,
    names it, and commits; the loser read the plan open, and its UPDATE,
    which waited for the winner's lock, closes it again, pointing at the
    winner's fact. Announced by the loser's pass, the plan had two events. The
    committed state is read in a session of its own, as
    services/vectors.py reads it after a late judgement: this transaction's
    reads see its own change. A row with no committed version was never seen
    open outside it."""
    if not facts:
        return []
    from ...db import get_own_sessionmaker

    with get_own_sessionmaker()() as other:
        committed = dict(other.query(AgentFact.id, AgentFact.invalid_at)
                         .filter(AgentFact.id.in_([f.id for f in facts])).all())
    return [f for f in facts if f.id in committed
            and (committed[f.id] is None or aware(committed[f.id]) > aware(f.invalid_at))]


def announce(db: Session, tenant_id: UUID, fact_id: str, invalidated_by: Optional[str],
             at: Optional[str], **more: Any) -> bool:
    """Enqueue the `fact.invalidated` of one closed fact (public ids), in the
    caller's transaction, unless this transaction already did. True when it
    was enqueued now."""
    from .. import webhooks

    said = db.info.setdefault(_ANNOUNCED, set())
    if fact_id in said:
        return False
    said.add(fact_id)
    payload: Dict[str, Any] = {"fact_id": fact_id, "invalidated_by": invalidated_by, **more,
                               "at": at}
    webhooks.emit_event(db, tenant_id, "fact.invalidated", payload)
    return True


def announce_the_rest(db: Session, tenant_id: UUID, at: Optional[str] = None) -> None:
    """Every fact this transaction closed and no write named: one event each,
    closed by the fact its row points at, or by none."""
    at = at or datetime.now(timezone.utc).isoformat()
    for f in closed_unannounced(db, tenant_id):
        announce(db, tenant_id, fact_public_id(f.id),
                 fact_public_id(f.invalidated_by_id) if f.invalidated_by_id else None, at)
