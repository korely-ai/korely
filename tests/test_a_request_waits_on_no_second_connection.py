"""A request that holds a connection never waits for a second one of the same pool.

Audit of 27 Oct 2026 (a request held a connection while the trail asked for
a second): a /v1 read kept its pooled connection until the request
ended, and the audit trail (services/audit.record) checked a second one out
of the same 15-connection pool for its row. With every connection held by
such reads, each waited for one of the others' to come back, and none did
until the checkout gave up 30 s later: every account stalled, and every
read's trail row was lost (AUDIT.lost). The writes made on the side, the
trail, the Cloud's counts, the model gate's read and the costs, now take
their connection from a pool of their own (db/base.get_own_sessionmaker,
core/threads.own_engine_pool), which the main pool's holders never wait on.

The main pool here is two connections, held by two reads at once: before
the fix both trail writes waited the whole checkout and wrote nothing.
"""
from __future__ import annotations

import threading
import time

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from tests.conftest import TEST_DB

HOLDERS = 2
CHECKOUT = 2.0   # the small pool's checkout wait: a trail row waiting for it is the bug


def test_reads_that_hold_the_whole_pool_still_write_their_trail(db, tenant, monkeypatch):
    import korely_agent.db as dbpkg
    from korely_agent.db.models import AuditEvent
    from korely_agent.services import usage

    small = create_engine(TEST_DB, future=True, pool_size=HOLDERS, max_overflow=0,
                          pool_timeout=CHECKOUT)
    Small = sessionmaker(bind=small, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(dbpkg, "get_sessionmaker", lambda: Small)
    together = threading.Barrier(HOLDERS)
    errors = []

    def a_read(i):
        s = Small()
        try:
            s.execute(text("SELECT 1"))     # the route's read: its connection held
            together.wait(10)               # every connection of the pool held now
            usage.log_v1_request(s, tenant.id, "memories:list", meta={"rows": i})
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)
        finally:
            s.close()

    started = time.monotonic()
    threads = [threading.Thread(target=a_read, args=(i,)) for i in range(HOLDERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    took = time.monotonic() - started
    small.dispose()

    assert not errors, errors
    rows = db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id).count()
    assert rows == HOLDERS, (
        f"{rows} of {HOLDERS} trail rows written: the trail waited for a connection "
        f"of the pool its own request held (took {took:.1f} s)")
    assert took < CHECKOUT, f"the trail waited {took:.1f} s for a connection"
