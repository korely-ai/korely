"""A request never waits on another transaction's lock with no end.

Audit of 20 Oct 2026 (a lock wait without a deadline): the engine every request
uses set no lock_timeout and no statement_timeout. A project's erasure holds
the project's row FOR UPDATE for its whole length, every write into the
project checks its foreign key against that row and waited, each holding a
request thread and a connection of the pool every account shares; fifteen of
them took the pool, and another account's key check answered 503 after the
pool's 30 s.

Now a request's transaction gives up a lock wait after
core/threads.LOCK_WAIT_SECONDS: 503 `unavailable` with Retry-After, its
connection back. The erasure itself waits as long as it needs
(`longer_deadlines`), and the worker's transactions keep no deadline.
"""
from __future__ import annotations

import contextvars
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from korely_agent.core import threads
from korely_agent.db.models import AgentProject
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service


@pytest.fixture
def short_lock_wait(monkeypatch):
    monkeypatch.setattr(threads, "LOCK_WAIT_SECONDS", 1, raising=False)
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


@pytest.fixture
def pilot(db, tenant):
    project_service.get_or_create_default_project(db, tenant.id)
    return project_service.create_project(db, tenant_id=tenant.id, name="Pilot")


def _held(engine, project_id):
    """A session holding the project's row as an erasure does."""
    s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    project_service._hold_the_project(s, s.get(AgentProject, project_id))
    return s


def test_a_write_into_a_held_project_gives_up_after_the_lock_wait(
        db, engine, tenant, pilot, short_lock_wait):
    from korely_agent.db.base import get_engine
    from korely_agent.main import create_app

    _row, key = key_service.create_api_key(db, tenant_id=tenant.id, name="w",
                                           scopes=["memories:read", "memories:write"],
                                           project_id=pilot.id)
    holder = _held(engine, pilot.id)
    # Let go after 8 s whatever happens: before the fix the write waited for
    # exactly this, and answered 201 when the holder let go.
    letting_go = threading.Timer(8.0, holder.rollback)
    letting_go.start()
    try:
        with TestClient(create_app()) as c:
            started = time.monotonic()
            r = c.post("/v1/memories", headers={"Authorization": f"Bearer {key}"},
                       json={"content": "user: I live in Turin.", "user_id": "anna"})
            waited = time.monotonic() - started
            assert r.status_code == 503, r.text
            assert r.json()["code"] == "unavailable"
            assert r.headers["Retry-After"] == str(threads.BUSY_RETRY_SECONDS)
            assert waited < 6, f"the write waited {waited:.1f} s on the lock"
            assert get_engine().pool.checkedout() == 0, "the write kept its connection"
            holder.rollback()
            r = c.post("/v1/memories", headers={"Authorization": f"Bearer {key}"},
                       json={"content": "user: I live in Turin.", "user_id": "anna"})
            assert r.status_code == 201, r.text
    finally:
        letting_go.cancel()
        holder.rollback()
        holder.close()


def _setting(on_a_request: bool, name: str) -> str:
    from korely_agent.db.base import get_sessionmaker

    def read():
        if on_a_request:
            threads.serving_a_request()
        s = get_sessionmaker()()
        try:
            # In milliseconds, whatever unit SHOW would pick.
            return s.execute(text("SELECT setting FROM pg_settings WHERE name = :n"),
                             {"n": name}).scalar_one()
        finally:
            s.close()

    return contextvars.copy_context().run(read)


def test_only_a_requests_transaction_has_the_deadlines(db, short_lock_wait):
    assert _setting(True, "lock_timeout") == "1000"
    assert _setting(True, "statement_timeout") == str(threads.STATEMENT_SECONDS * 1000)
    # The worker's: none, and nothing left on the pooled connection either.
    assert _setting(False, "lock_timeout") == "0"
    assert _setting(False, "statement_timeout") == "0"


def test_the_erasure_itself_waits_for_the_write_under_way(db, engine, tenant, pilot,
                                                         short_lock_wait):
    """An erasure is a request too: it waits past a request's lock wait for
    the write under way into the project, then erases."""
    from korely_agent.db.base import get_sessionmaker

    pid = pilot.id
    holder = _held(engine, pid)
    out = {}

    def erase():
        threads.serving_a_request()
        s = get_sessionmaker()()
        try:
            out["done"] = project_service.delete_project(
                s, tenant.id, s.get(AgentProject, pid), erase=True)
        except Exception as e:  # noqa: BLE001 - reported below
            out["error"] = e
        finally:
            s.close()

    t = threading.Thread(target=contextvars.copy_context().run, args=(erase,))
    t.start()
    time.sleep(2.0)       # twice the request's lock wait
    holder.rollback()
    holder.close()
    t.join(30)
    assert "error" not in out, repr(out.get("error"))
    assert out["done"]["audit_id"]
    db.expire_all()
    assert db.query(AgentProject).filter(AgentProject.id == pid).first() is None


def test_a_deadline_is_told_from_other_database_errors():
    class Orig:
        def __init__(self, code):
            self.pgcode = code

    class Err(Exception):
        def __init__(self, code):
            self.orig = Orig(code)

    assert threads.deadline_passed(Err("55P03")) and threads.deadline_passed(Err("57014"))
    assert not threads.deadline_passed(Err("40P01")) and not threads.deadline_passed(Err(None))
    assert not threads.deadline_passed(ValueError(str(uuid.uuid4())))
