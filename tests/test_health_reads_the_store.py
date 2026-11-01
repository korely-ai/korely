"""/health's `extraction` reads the store as well as this process's calls
(the audit of 2026-10-06, row 3).

With the worker in another process (RUN_WORKER=false) the API never
extracts, and `extraction` stayed `unknown` whatever the worker did; and a
drain that stopped without a failed call had nothing to show. The store
knows both: the oldest memory still waiting, and the last extraction that
answered (its usage row). No model is called.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.core import llm
from korely_agent.db.models import AgentLlmUsage, AgentMemory



@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient

    from korely_agent.config import get_settings
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setattr(llm, "provider_configured", lambda: True)
    get_settings.cache_clear()
    llm._last_call.update({"ok": None, "at": None, "error": None})
    with TestClient(create_app()) as c:
        yield c
    llm._last_call.update({"ok": None, "at": None, "error": None})
    get_settings.cache_clear()


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


def _waiting(db, tenant, since):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                    content="Maria moved to Milan.", graph_sync_status="pending")
    db.add(m)
    db.commit()
    m.created_at = since
    db.commit()


def _extracted(db, tenant, when):
    db.add(AgentLlmUsage(tenant_id=tenant.id, stage="extraction", provider="mistral",
                         model="gpt-oss-120b", ts=when))
    db.commit()


def test_the_worker_elsewhere_is_seen_working(db, tenant, client):
    _extracted(db, tenant, _ago(seconds=40))
    ex = client.get("/health").json()["extraction"]
    assert ex["state"] == "working" and 30 <= ex["seconds_ago"] < 120


def test_nothing_mining_a_queue_is_failing(db, tenant, client):
    _waiting(db, tenant, _ago(minutes=12))
    _extracted(db, tenant, _ago(minutes=20))
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["extraction"]["state"] == "failing"
    assert "nothing is mining them" in body["extraction"]["detail"]


def test_a_backlog_being_worked_through_is_not_a_failure(db, tenant, client):
    _waiting(db, tenant, _ago(minutes=40))
    _extracted(db, tenant, _ago(seconds=20))
    assert client.get("/health").json()["extraction"]["state"] == "working"


def test_this_process_failing_still_says_so(db, tenant, client):
    _extracted(db, tenant, _ago(seconds=20))
    llm._record(False, "ClientError: 429 prepayment credits are depleted")
    ex = client.get("/health").json()["extraction"]
    assert ex["state"] == "failing" and "429" in ex["detail"]


def test_a_store_that_cannot_be_read_is_degraded_not_a_500(client, monkeypatch):
    def down():
        raise RuntimeError("could not connect to server")

    monkeypatch.setattr("korely_agent.db.get_sessionmaker", down)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded" and r.json()["database"]["state"] == "failing"


def test_with_no_writer_the_store_is_still_read(client, monkeypatch):
    """Audit of 17 Oct 2026: the store was read only with a writer,
    and with Postgres down the writer saved on the Models page cannot be
    found either, so a restart with the database stopped said "ok, not
    configured: memories are stored, readable and waiting"."""
    def down():
        raise RuntimeError("could not connect to server")

    monkeypatch.setattr(llm, "provider_configured", lambda: False)
    monkeypatch.setattr("korely_agent.db.get_sessionmaker", down)
    body = client.get("/health").json()
    assert body["status"] == "degraded" and body["database"]["state"] == "failing"
    assert body["extraction"]["state"] == "unknown"
    assert "waiting" not in body["extraction"]["detail"]


def test_with_no_writer_and_the_store_up_it_is_ok_and_waiting(db, tenant, client, monkeypatch):
    """No key is a correct install (README): memories wait, nothing is
    failing, however long they have waited."""
    monkeypatch.setattr(llm, "provider_configured", lambda: False)
    _waiting(db, tenant, _ago(minutes=40))
    body = client.get("/health").json()
    assert body["status"] == "ok" and "database" not in body
    assert body["extraction"]["state"] == "not configured"
