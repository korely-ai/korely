"""An admission job never waits on a lock one account's own transaction holds.

Audit of 27 Oct 2026 (the admission pool held a thread through a lock
wait): the MCP's key check runs on the admission pool, four threads every
account's key checks, counters and OAuth steps share, and stamped the key's
`last_used_at` with an UPDATE that waited for the key's row. An account's
own project deletion or erasure holds that row for its whole length, so four
key checks of that account held the four threads for a request's lock wait
(5 s) each, and every other account's key check queued behind them. The
stamp now skips a row another transaction holds (services/keys), and every
admission job gives up any other lock wait after ADMISSION_LOCK_WAIT_SECONDS
(core/threads.with_deadlines).
"""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import text

from korely_agent.core import threads
from korely_agent.services import keys as key_service

PROMPT = 1.0


@pytest.fixture
def real_engine(monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.config import get_settings
    from korely_agent.db import base

    get_settings.cache_clear()
    base.get_engine.cache_clear()
    base.get_sessionmaker.cache_clear()
    yield
    base.get_engine().dispose()
    base.get_engine.cache_clear()
    base.get_sessionmaker.cache_clear()
    get_settings.cache_clear()


def _tenant(db, name):
    import uuid

    from korely_agent.db.models import Tenant

    t = Tenant(id=uuid.uuid4(), name=name)
    db.add(t)
    db.commit()
    return t


def test_another_accounts_key_check_is_prompt_while_one_accounts_key_row_is_held(
        db, engine, real_engine):
    from sqlalchemy.orm import sessionmaker

    from korely_agent.mcp import server as mcp_server

    a, b = _tenant(db, "A"), _tenant(db, "B")
    row_a, key_a = key_service.create_api_key(db, tenant_id=a.id, name="a",
                                              scopes=["memories:read"])
    _row_b, key_b = key_service.create_api_key(db, tenant_id=b.id, name="b",
                                               scopes=["memories:read"])
    # Account A's own long transaction (a project deletion moves its keys)
    # holds its key's row.
    holder = sessionmaker(bind=engine)()
    holder.execute(text("SELECT 1 FROM agent_api_keys WHERE id = :i FOR UPDATE"),
                   {"i": row_a.id})
    try:
        async def go():
            threads.serving_a_request()
            verify = mcp_server.KorelyAgentKeyVerifier().verify_token
            a_checks = [asyncio.create_task(verify(key_a))
                        for _ in range(threads.ADMISSION_THREADS)]
            await asyncio.sleep(0.2)          # A's checks on every admission thread
            started = time.monotonic()
            token = await asyncio.wait_for(verify(key_b), 15)
            took = time.monotonic() - started
            a_tokens = await asyncio.gather(*a_checks)
            return token, took, a_tokens

        token, took, a_tokens = asyncio.run(go())
    finally:
        holder.rollback()
        holder.close()
    assert token is not None and token.subject == str(b.id)
    assert all(t is not None for t in a_tokens), "A's key is fine: let in, stamp skipped"
    assert took < PROMPT, f"account B's key check waited {took:.1f} s behind A's held row"


def test_an_admission_job_gives_up_a_lock_wait_sooner_than_a_request(db, engine, tenant,
                                                                     real_engine):
    """Any other row an admission job waits on (OAuth's project, read under
    its lock): the job gives up after ADMISSION_LOCK_WAIT_SECONDS, and the
    door answers 503 `unavailable` as for any deadline."""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import sessionmaker

    from korely_agent.db import get_sessionmaker

    holder = sessionmaker(bind=engine)()
    holder.execute(text("SELECT 1 FROM tenants WHERE id = :i FOR UPDATE"), {"i": tenant.id})

    def the_job():
        s = get_sessionmaker()()
        started = time.monotonic()
        try:
            s.execute(text("SELECT 1 FROM tenants WHERE id = :i FOR UPDATE"), {"i": tenant.id})
            return "got it", time.monotonic() - started
        except OperationalError as e:
            return threads.deadline_passed(e), time.monotonic() - started
        finally:
            s.rollback()
            s.close()

    try:
        async def go():
            threads.serving_a_request()
            return await threads.admission_off_the_loop(the_job)

        gave_up, took = asyncio.run(go())
    finally:
        holder.rollback()
        holder.close()
    assert gave_up is True, gave_up
    assert took < threads.LOCK_WAIT_SECONDS / 2, f"the admission job waited {took:.1f} s"
    assert threads.ADMISSION_LOCK_WAIT_SECONDS < threads.LOCK_WAIT_SECONDS
