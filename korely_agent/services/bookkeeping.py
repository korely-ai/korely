"""Whether the product's own records are being kept, for /health.

Two records feed decisions and are written apart from the work they describe,
so that a failure to write them never fails the work: the cost rows
(`llm_usage.flush`, `agent_llm_usage`) and the audit trail (`audit.record`,
`audit_events`). Each catches its own failure and logs it, and that was the
whole trace. The Cloud's daily emergency stop sums the cost rows
(hosted/spending.py), so a cost row lost lowers the spending it reads and
raises the stop without anybody seeing it; an audit row lost is a hole in
the trail sold as the answer to "who read this", found only by reading the
container's log (audit of 17 Oct 2026).

Now each write says how it went here, and /health reports it under
`records`: `working` when the last write went in, `failing` when it did not
(the status reads degraded), `unknown` before the first one; with how many
were lost since the process started. In-process, like the worker's jobs: a
restart starts from zero. The failure is told by its class, never its
message, which can quote a row.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional


class Ledger:
    """One record's writes in this process: the last outcome and the losses."""

    def __init__(self, name: str, dropped_as: str = "dropped_with_their_account"):
        self.name = name
        # The key `state` counts the rows dropped under (`dropped`).
        self.dropped_as = dropped_as
        self._lock = threading.Lock()
        self._clear()

    def _clear(self) -> None:
        self._last_ok: Optional[bool] = None
        self._last_at: Optional[datetime] = None
        self._lost = 0
        self._lost_at: Optional[datetime] = None
        self._failure: Optional[str] = None
        self._dropped = 0
        self._gave_way = 0
        self._gave_way_at: Optional[datetime] = None

    def kept(self) -> None:
        """A write went in."""
        with self._lock:
            self._last_ok, self._last_at = True, datetime.now(timezone.utc)

    def lost(self, n: int, failure: BaseException) -> None:
        """`n` rows could not be written."""
        now = datetime.now(timezone.utc)
        with self._lock:
            self._last_ok, self._last_at = False, now
            self._lost += int(n)
            self._lost_at = now
            self._failure = type(failure).__name__

    def dropped(self, n: int) -> None:
        """`n` rows not written because what they belong to is gone (an
        account closed during its calls): no failure, but not counted
        either, so it is said."""
        with self._lock:
            self._dropped += int(n)

    def gave_way(self, n: int) -> None:
        """`n` rows not written because the write gave way: one of its
        deadlines passed (core/threads.deadline_passed), waiting for another
        transaction's lock or on a statement the database ran too slowly. The
        work it waited on goes first, by design, and the install is not
        failing; counted apart, so it is said."""
        with self._lock:
            self._gave_way += int(n)
            self._gave_way_at = datetime.now(timezone.utc)

    def state(self) -> dict:
        with self._lock:
            if self._last_ok is None:
                out = {"state": "unknown", "detail": "nothing written in this process yet"}
            elif self._last_ok:
                out = {"state": "working"}
            else:
                out = {"state": "failing",
                       "detail": f"the last write failed ({self._failure}); "
                                 f"`docker compose logs api` has the stack"}
            if self._last_at is not None:
                out["seconds_ago"] = _ago(self._last_at)
            if self._lost:
                out["lost"] = self._lost
                out["lost_seconds_ago"] = _ago(self._lost_at)
            if self._dropped:
                out[self.dropped_as] = self._dropped
            if self._gave_way_at is not None:
                out["gave_way"] = self._gave_way
                out["gave_way_seconds_ago"] = _ago(self._gave_way_at)
            return out

    def used(self) -> bool:
        """Whether anything was written, lost or given way in this process."""
        with self._lock:
            return self._last_ok is not None or self._gave_way_at is not None

    def reset(self) -> None:
        """For tests."""
        with self._lock:
            self._clear()


def _ago(when: datetime) -> int:
    return int((datetime.now(timezone.utc) - when).total_seconds())


#: The cost rows of the model calls (`llm_usage.flush`), counted in calls.
COSTS = Ledger("costs")
#: The audit trail (`audit.record`), counted in events.
AUDIT = Ledger("audit")
#: The Cloud's counts given back (hosted/quotas._give_back: a write that did
#: not commit, a read that was not answered), counted in writes and queries:
#: one that failed leaves the account charged for what it never had.
GIVEN_BACK = Ledger("given_back")
#: The read marks of the agent notices (services/read_marks.py), counted in
#: facts: one lost is a change its reader will not be told of; one dropped
#: named a fact, a project or an account deleted while it was written; one
#: that gave way waited past its fifth of a second for a deletion or a
#: closure holding the row, or past its second on a slow statement (and is
#: no failure: /health stays ok).
READ_MARKS = Ledger("read_marks", dropped_as="dropped_with_what_they_name")

# Shown only once written in this process: only the Cloud gives counts back,
# and only a read that names its reader writes a mark, so /health answers
# as before to an install that does neither.
_ONCE_USED = (GIVEN_BACK, READ_MARKS)


def state() -> dict:
    """{"costs": ..., "audit": ...} for /health, and "given_back" and
    "read_marks" once one was written in this process."""
    out = {COSTS.name: COSTS.state(), AUDIT.name: AUDIT.state()}
    for ledger in _ONCE_USED:
        if ledger.used():
            out[ledger.name] = ledger.state()
    return out
