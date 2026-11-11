"""/health reports the worker's batch, webhooks, purge and keys jobs (audit
of 17 Oct 2026). They recorded nothing: one failing on every tick left
/health saying "ok", where the export already had its own record."""
import os

import pytest
from fastapi.testclient import TestClient

from korely_agent import config, worker
from korely_agent.db import base


@pytest.fixture
def health(monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    # The suite's own database when there is one: an engine made here on a
    # made-up URL stays cached for every test after it.
    monkeypatch.setenv("DATABASE_URL", os.environ.get("DATABASE_URL") or "postgresql://x:x@localhost/x")
    monkeypatch.delenv("FACTS_EXPORT_DIR", raising=False)
    monkeypatch.setattr(worker, "_job_state", {})
    # No database behind these: the session is never used by the jobs below.
    monkeypatch.setattr(worker, "get_sessionmaker",
                        lambda: lambda: type("S", (), {"close": lambda s: None,
                                                       "rollback": lambda s: None})())
    config.get_settings.cache_clear()
    from korely_agent.main import create_app

    def get():
        with TestClient(create_app()) as c:
            return c.get("/health").json()
    yield get
    config.get_settings.cache_clear()
    base.get_sessionmaker.cache_clear()
    base.get_engine.cache_clear()


def test_before_any_run_each_is_unknown(health):
    body = health()
    assert set(body["jobs"]) == {"batch", "webhooks", "purge", "keys"}
    assert all(j["state"] == "unknown" for j in body["jobs"].values())


def test_a_job_that_raises_is_failing_and_degrades_the_status(health):
    def webhooks(db):
        raise RuntimeError("delivery to anna@example.com refused")

    worker._run(webhooks, "webhooks")
    worker._run(lambda db: {"processed": 0}, "batch")
    body = health()
    assert body["jobs"]["webhooks"]["state"] == "failing"
    assert "RuntimeError" in body["jobs"]["webhooks"]["detail"]
    # The class, never the message: it can quote a row.
    assert "anna@example.com" not in str(body)
    assert body["jobs"]["batch"]["state"] == "working"
    assert body["status"] == "degraded"


def test_a_job_that_recovers_is_working_again(health):
    def boom(db):
        raise OSError("disk")

    worker._run(boom, "purge")
    worker._run(lambda db: {}, "purge")
    body = health()
    assert body["jobs"]["purge"]["state"] == "working"
    assert body["jobs"]["purge"]["seconds_ago"] >= 0


def test_a_lost_cost_row_or_audit_row_degrades_the_status(health):
    """The cost rows and the audit trail are written apart from the work and
    swallow their own failures: a log line was the whole trace (audit of 17
    Oct 2026). /health says so under `records`."""
    from korely_agent.services import bookkeeping

    bookkeeping.COSTS.reset()
    bookkeeping.AUDIT.reset()
    try:
        body = health()
        assert {r["state"] for r in body["records"].values()} <= {"unknown", "working"}
        bookkeeping.AUDIT.lost(1, RuntimeError("row of anna@example.com"))
        body = health()
        assert body["records"]["audit"]["state"] == "failing"
        assert body["records"]["audit"]["lost"] == 1
        assert "anna@example.com" not in str(body)
        assert body["status"] == "degraded"
    finally:
        bookkeeping.COSTS.reset()
        bookkeeping.AUDIT.reset()


def test_an_audit_write_that_fails_is_counted(monkeypatch):
    import uuid

    from korely_agent.services import audit, bookkeeping

    class Broken:
        def add(self, *a, **k):
            raise RuntimeError("database is on fire")

        def rollback(self):
            pass

        def close(self):
            pass

    bookkeeping.AUDIT.reset()
    monkeypatch.setattr("korely_agent.db.get_sessionmaker", lambda: (lambda: Broken()))
    try:
        audit.record(None, uuid.uuid4(), "read", project_id=None)
        state = bookkeeping.state()["audit"]
        assert state["state"] == "failing" and state["lost"] == 1, state
    finally:
        bookkeeping.AUDIT.reset()
