"""PATCH /v1/memories/{id} follows WRITE_ASYNC, as POST does.

It extracted inline whatever the setting said: with WRITE_ASYNC=true (the
default, "an agent in a conversation cannot wait eight seconds") an edit held
the request open for the whole model call that a write had been spared. Now
the edit leaves the memory pending and the worker mines it, dated by the
memory's own event time; with WRITE_ASYNC=false it is mined inline, dated the
same way, and the reply says `ready` once it is.
"""
import uuid
from datetime import datetime, timezone

import pytest

from korely_agent.db.models import AgentMemory
from korely_agent.services import keys as key_service
from korely_agent.services.facts import service as facts_service


MARCH = datetime(2026, 3, 1, tzinfo=timezone.utc)


@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient
    from korely_agent import config
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    config.get_settings.cache_clear()
    try:
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()
    finally:
        config.get_settings.cache_clear()


@pytest.fixture
def head(db, tenant):
    _row, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}",
        scopes=["memories:read", "memories:write"])
    return {"Authorization": f"Bearer {full}"}


@pytest.fixture
def inline(monkeypatch):
    """The extraction the request makes itself, recorded: which text, dated
    how. It succeeds, as a model that answered would."""
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
    seen = []

    def spy(db_, tenant_id, *, memory, valid_from=None, **k):
        seen.append((memory.content, valid_from))
        memory.graph_sync_status = "done"
        db_.commit()
        return []

    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts", spy)
    return seen


@pytest.fixture
def worker(monkeypatch, db):
    """The drain, with its extraction recorded the same way."""
    seen = []

    def spy(db_, tenant_id, *, memory, valid_from=None, **k):
        seen.append((memory.content, valid_from))
        memory.graph_sync_status = "done"
        db_.commit()
        return []

    def run():
        monkeypatch.setattr(facts_service, "extract_and_persist_facts", spy)
        facts_service.process_pending_memories(db, limit=10)
        return seen

    return run


def _writes(monkeypatch, asynchronous: bool):
    # Through the real settings, which every door reads (the write is
    # services/memory_write.py's since the audit of 17 Oct 2026).
    from korely_agent.config import get_settings

    monkeypatch.setenv("WRITE_ASYNC", "true" if asynchronous else "false")
    get_settings.cache_clear()


def _write_and_settle(client, head, worker):
    """A memory from March whose first extraction is behind it."""
    r = client.post("/v1/memories", headers=head, json={
        "content": "Maria lives in Rome.", "user_id": "maria", "timestamp": "2026-03-01"})
    assert r.status_code == 201, r.text
    worker()
    return r.json()["id"]


class TestAsynchronous:
    def test_the_edit_returns_without_extracting_and_the_worker_mines_it(
            self, client, head, db, inline, worker, monkeypatch):
        _writes(monkeypatch, asynchronous=True)
        mid = _write_and_settle(client, head, worker)
        worker().clear()

        r = client.patch(f"/v1/memories/{mid}", headers=head,
                         json={"content": "Maria lives in Milan."})
        assert r.status_code == 200, r.text
        assert inline == [], "the edit extracted inline with WRITE_ASYNC=true"
        body = r.json()
        assert (body["status"], body["facts"]) == ("processing", [])
        assert body["content"] == "Maria lives in Milan."
        db.expire_all()
        assert db.query(AgentMemory).one().graph_sync_status == "pending"

        assert worker() == [("Maria lives in Milan.", MARCH)], (
            "the worker did not mine the edit, or dated it on the day it ran")
        db.expire_all()
        assert client.get(f"/v1/memories/{mid}", headers=head).json()["status"] == "ready"


class TestSynchronous:
    def test_the_edit_is_mined_inline_and_dated_like_the_worker_would(
            self, client, head, db, inline, worker, monkeypatch):
        _writes(monkeypatch, asynchronous=False)
        r = client.post("/v1/memories", headers=head, json={
            "content": "Maria lives in Rome.", "user_id": "maria", "timestamp": "2026-03-01"})
        mid = r.json()["id"]
        inline.clear()

        r = client.patch(f"/v1/memories/{mid}", headers=head,
                         json={"content": "Maria lives in Milan."})
        assert r.status_code == 200, r.text
        assert inline == [("Maria lives in Milan.", MARCH)], (
            "the inline extraction of an edit dated its facts differently from "
            "the worker's: the two modes gave two histories")
        assert r.json()["status"] == "ready", (
            "the reply was serialised before the extraction it waited for")
        assert worker() == [], "nothing is left for the worker"
