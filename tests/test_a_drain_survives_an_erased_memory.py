"""A memory erased, or a session broken, during its extraction does not take
the drain down (memory_core 5.2.3).

Code audit findings: LATE-03 (a DELETE /v1/agents/{id} during an extraction:
`drain_one` read `m.graph_sync_status`, then `m.id` in its except, on a row
that was gone, and ObjectDeletedError left the drain with the rest of the
group 'processing' for 15 minutes) and the account closings seen
on 0.1.38 and 0.1.39 (a StaleDataError from the vocabulary, then a
PendingRollbackError from reading `m.id` on a session that needed a
rollback).

The production session does not expire its objects on commit, any more than
this suite's (db/base.py and tests/conftest.py, both expire_on_commit=False).
What leaves the drain holding an expired object for a row that is gone is a
rollback, which expires everything the session holds (the write pipeline
rolls back on a failure), or a row deleted by another session and then read
again. The tests expire the object by hand to stand for that moment; it is
not a setting of production (the sentence of before came from another
session's setup; audit of 18 Oct 2026).
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import text

from korely_agent.db.models import AgentMemory
from korely_agent.services.memory_core import worker as lav


LOGGER = "korely_agent.services.memory_core.worker"


def _taken(db, tenant):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="maria",
                    content="Maria works at Acme.", graph_sync_status="pending")
    db.add(m)
    db.commit()
    lav.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
    return m


def _erase(db, m):
    """What an erasure does from elsewhere: the row is deleted and committed,
    and the drain's session holds an expired object for it (as after a
    rollback: see the module's docstring)."""
    db.execute(text("DELETE FROM agent_memories WHERE id = :id"), {"id": m.id})
    db.commit()
    db.expire(m)


def test_a_memory_erased_during_a_write_that_ends_is_no_longer_this_drains(db, tenant, caplog):
    m = _taken(db, tenant)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert lav.drain_one(db, m, lambda mm, ev: _erase(db, mm)) is True
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_memory_erased_during_a_write_that_fails_is_logged_as_ended_not_failed(db, tenant, caplog):
    m = _taken(db, tenant)

    def fail_it(mm, ev):
        _erase(db, mm)
        raise RuntimeError("UPDATE expected to update 1 row(s); 0 were matched")

    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert lav.drain_one(db, m, fail_it) is True
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    [line] = [r.getMessage() for r in caplog.records if "no longer this drain's" in r.getMessage()]
    assert "RuntimeError" in line and "0 were matched" not in line


def test_a_session_left_needing_a_rollback_is_rolled_back_and_the_memory_filed(db, tenant, caplog):
    m = _taken(db, tenant)
    mid = m.id

    def fail_it(mm, ev):
        db.expire(mm)
        db.execute(text("SELECT 1/0"))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert lav.drain_one(db, m, fail_it) is False
    db.expire_all()
    assert db.get(AgentMemory, mid).graph_sync_status == "error"
    assert [r for r in caplog.records if "async pending process failed" in r.getMessage()]
