"""This process's identity as the holder of the work it claims, kept alive
by the database for as long as the process is connected to it (1.0.22).

The facts drain claims memories ('processing', `graph_sync_claimed_at`), and
until 1.0.22 the clock was the only way to tell a claim whose process had
ended from one still being worked: the reaper gives a claim back after
DRAIN_STALE_MINUTES (memory_core `worker.claim_pending`). So every restart
(a deploy, a rollback, a crash) left the group being extracted 'processing'
for those 15 minutes, and its person with it (facts/drain
`_persons_in_hand`), the memories written after the restart included (the
Cloud's deploy of 1.0.19, 13 Nov 2026).

A process here holds a session-level advisory lock, (SPACE, key), on a
connection of its own that never goes back to a pool. PostgreSQL keeps it
for exactly that session: "Once acquired at session level, an advisory lock
is held until explicitly released or the session ends" (PostgreSQL 17
documentation, section 13.3.5, Advisory Locks), and a process that ends,
cleanly or killed, ends its sessions with it (its sockets close). The
`pg_locks` view shows every session's advisory locks to every role, with the
two keys in `classid` and `objid`, `objsubid` 2, and the database they belong
to ("Advisory locks are local to each database", section 52.12, pg_locks).
So a claim stamped with a key that no session of this database holds is a
process's that has ended, and can go back to the queue at once; one stamped
with a key a session holds is a live process's, and the liveness check never
gives it back. A claim with no stamp is left to the clock, as before; and
the clock (memory_core's reaper, DRAIN_STALE_MINUTES) still takes back any
claim older than that, stamped or not, as it always did.

The lock is the liveness, not a heartbeat on a clock: a heartbeat would be
the same guess with a shorter timeout, and a process slower than it would
lose a claim it is still working. What the lock cannot tell is a process
that keeps running after its own connection was cut (the server restarted,
an administrator ended the session, `idle_session_timeout`): until it takes
its key again it reads as ended, and another process's drain may give its
claims back and claim them. The process takes its key again at the start of
its next claim, before it gives back anything itself, and gives back
nothing while it holds no key (facts/drain `_claim`), so its own drain never
takes its own claims; and each of its groups works
only the claims its tick made (facts/drain `_run_group`): at the first
memory claimed since by another process it stops, and the write of the
memory it was extracting is discarded by the claim token (memory_core
`worker.Claim`, a compare-and-set on what the claim read). Time lost, one
more model call, never a wrong write.

The lock lives on the session that took it, so the connection must be a
direct one to PostgreSQL: behind a pooler in transaction mode (PgBouncer's
`pool_mode = transaction`) the next statement may run on another server
session. The holder checks that its own session holds its key, not only
that the connection answers, and takes a key again when it does not.

The connection comes from the own sessions' engine (db/base
`get_own_engine`), never from the pool of the session that asks: the tick
that stamps holds a connection of the main pool, and asking that pool for a
second one is the nested checkout the audit of 27 Oct 2026 removed. It is
detached at once, so it is no pool's afterwards.

The key is random, positive, below 2**31 (the second key of the two-int
form is an `integer`), tried with `pg_try_advisory_lock` until one is free:
two processes never share one. The two-key space is apart from the one-key
locks `hashtext()` takes elsewhere (objsubid 1).
"""
from __future__ import annotations

import logging
import secrets
import threading
from typing import Any, Callable, Optional

from sqlalchemy import column, exists, func, select, table, text

from .memory_core import log_safe

logger = logging.getLogger(__name__)

# The first key of every holder's lock: the space of this lock alone. Any
# number would do that no other two-key lock of the product uses (none does).
SPACE = 1802465893
# A key is taken at random; a few tries find a free one among a handful of
# processes on one database.
_TRIES = 8

# A request waiting for a key shows there too, but only while a session holds it.
_PG_LOCKS = table("pg_locks", column("locktype"), column("database"), column("classid"),
                  column("objid"), column("objsubid"))
_PG_DATABASE = table("pg_database", column("oid"), column("datname"))
# The holder's own session holds its key (`Holder._still_held`).
_HELD_HERE = text(
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted"
    " AND pid = pg_backend_pid() AND classid = :space AND objid = :key AND objsubid = 2)")


def alive(key_column: Any):
    """SQL: a session of this database holds the holder's key in
    `key_column` (a claim's stamp), so the process that made the claim is
    still connected."""
    this_database = (select(_PG_DATABASE.c.oid)
                     .where(_PG_DATABASE.c.datname == func.current_database())
                     .scalar_subquery())
    return exists().where(
        _PG_LOCKS.c.locktype == "advisory",
        _PG_LOCKS.c.database == this_database,
        _PG_LOCKS.c.classid == SPACE,
        _PG_LOCKS.c.objid == key_column,
        _PG_LOCKS.c.objsubid == 2,
    )


class Holder:
    """One process's key, its connection and whether it has given its claims
    back. Every use is under `lock`, so a stamp and the giving back never
    cross: a claim stamped after the giving back is given back by the stamp
    itself (facts/drain `_stamped`)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._conn = None
        self._key: Optional[int] = None
        self._closed = False

    @property
    def closed(self) -> bool:
        """The process gave its claims back: it claims and stamps no more."""
        return self._closed

    def key(self, bind) -> Optional[int]:
        """The key this process's session holds now, on the database of
        `bind` (the asker's session, read for its dialect only), taken, or
        taken again when its session no longer holds it, on first use. None
        when the database is not PostgreSQL or no key is held (the claims
        are then left to the clock): never a key no session holds. Called
        under `lock`, by a caller that has checked `closed`."""
        if getattr(bind, "engine", bind).dialect.name != "postgresql":
            return None
        if self._conn is not None and self._still_held():
            return self._key
        try:
            got = self._take()
        except Exception as e:  # noqa: BLE001 - without a key the clock decides, as before
            self._drop()
            logger.error("[holder] no key for this process's claims, they wait for the "
                         "clock: %s", log_safe.error(e))
            return None
        if got is not None:
            # The one to try first next time, too (`_take`).
            self._key = got
        return got

    def _still_held(self) -> bool:
        """This connection's own session holds the key: the connection
        answers, and the lock is on its backend (`pg_backend_pid`). A
        connection that answers from another session (a pooler in front)
        holds nothing for this process. Each read is committed, so the
        session never idles inside a transaction."""
        try:
            held = self._conn.execute(_HELD_HERE, {"space": SPACE, "key": self._key}).scalar()
            self._conn.commit()
        except Exception as e:  # noqa: BLE001 - a cut connection is taken again
            logger.warning("[holder] this process's connection was cut, its key is taken "
                           "again: %s", log_safe.error(e))
            self._drop()
            return False
        if not held:
            logger.error("[holder] this process's session no longer holds its key (is the "
                         "database behind a pooler in transaction mode?): taken again")
            self._drop()
        return bool(held)

    def _take(self) -> Optional[int]:
        """A connection of its own, and a free key on it: the one held
        before first, so the claims stamped with it stay this process's."""
        # The own sessions' engine, not the asker's: see the module's docstring.
        from ..db.base import get_own_engine

        conn = get_own_engine().connect()
        # Out of the pool for good (SQLAlchemy `Connection.detach`): its
        # session holds the lock, and a pooled connection would hand the lock
        # to whoever takes it next. Closing it ends the session.
        conn.detach()
        self._conn = conn
        tries = ([self._key] if self._key else []) + [
            secrets.randbelow(2**31 - 1) + 1 for _ in range(_TRIES)]
        for key in tries:
            got = conn.execute(text("SELECT pg_try_advisory_lock(:space, :key)"),
                               {"space": SPACE, "key": key}).scalar()
            conn.commit()
            if got:
                return key
        self._drop()
        logger.error("[holder] no free key in %d tries: this process's claims wait for the "
                     "clock", len(tries))
        return None

    def give_back(self, back: Callable[[Any, int], int]) -> int:
        """The process's last act: no more stamps, `back(connection, key)`
        gives back what this key holds, on the holder's own connection, and
        the connection closes, which ends the lock. How many `back` gave;
        never raises."""
        with self.lock:
            self._closed = True
            if self._conn is None or self._key is None:
                return 0
            n = 0
            try:
                n = int(back(self._conn, self._key) or 0)
            except Exception as e:  # noqa: BLE001 - the next process gives them back
                logger.error("[holder] could not give this process's claims back, the next "
                             "start does: %s", log_safe.error(e))
            self._drop()
            return n

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - a connection already gone
            pass
