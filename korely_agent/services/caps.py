"""The most of a kind of row an account may hold, counted one write at a time.

An account holds at most so many keys (keys.MAX_API_KEYS), projects
(projects.MAX_PROJECTS), webhook endpoints (webhooks.MAX_ENDPOINTS) and
declared names (facts/entities.MAX_DECLARED_NAMES). Each cap was its own copy
of the same four steps (the audit of 27 Oct 2026): the account's lock of that
kind, the count, the rollback, the refusal; the copies had begun to differ,
and the next cap would have been a fifth. Here once.

The lock is a transaction's (`pg_advisory_xact_lock`, released by the commit
or the rollback), so two writes that counted together cannot both go in past
the cap (the audit of 20 Oct 2026: a webhook's count was not locked), and it
is waited on within a request's lock deadline (core/threads.with_deadlines).
"""
from __future__ import annotations

from typing import Callable, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


def room_for_one_more(db: Session, kind: str, tenant_id: UUID,
                      held: Callable[[], Optional[int]], most: int,
                      refused: Callable[[int], Exception]) -> Optional[int]:
    """Hold the account's lock of `kind` to the commit, then count: `held()`,
    the rows of that kind the account holds now (None when the write adds
    none, which is never refused). At `most` or past it, the transaction is
    rolled back and `refused(held)` raised. The count, when there is one."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"{kind}:{tenant_id}"})
    n = held()
    if n is not None and n >= most:
        db.rollback()
        raise refused(n)
    return n
