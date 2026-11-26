"""A memory its write extracts before answering is claimed in the commit that
writes it, so the drain does not extract it a second time (the audit of
2026-10-06, row 1).

The drain runs in the same process every ten seconds, and without a pause
when there is a backlog. A memory written 'pending' and then extracted inline
was there for it to take during the model call: two calls, two judgements of
the same facts, and the second extraction closing the facts of the first that
it did not repeat. The drain is made to tick inside the model call here, as
it does in production, and no model is called.
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from korely_agent.db.models import AgentMemory
from korely_agent.services import keys as key_service
from korely_agent.services.facts import service as S
from korely_agent.services.memory_core.fact_extractor import Extraction
from tests.conftest import seeded_project_id



@pytest.fixture
def writes(monkeypatch):
    def set_async(value: bool):
        from korely_agent.config import get_settings

        monkeypatch.setenv("WRITE_ASYNC", "true" if value else "false")
        get_settings.cache_clear()
    yield set_async
    from korely_agent.config import get_settings

    get_settings.cache_clear()


@pytest.fixture
def model(monkeypatch, engine):
    """The extraction call, recorded; on its first call the drain ticks in
    another session, as the worker does while a write waits on the model.
    `fail` makes the call raise."""
    calls, state = [], {"inside": False, "fail": False}
    other = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

    def extraction(content, **kw):
        calls.append(content)
        if state["fail"]:
            raise RuntimeError("the model did not answer")
        if not state["inside"]:
            state["inside"] = True
            try:
                S.process_pending_memories(other, limit=20)
            finally:
                state["inside"] = False
        return Extraction([], None)

    monkeypatch.setattr(S, "extract_facts_and_observation", extraction)
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
    yield calls, state
    other.close()


@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient

    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _head(db, tenant):
    _r, key = key_service.create_api_key(db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}")
    return {"Authorization": f"Bearer {key}"}


def _status(db, mid):
    db.expire_all()
    return db.get(AgentMemory, mid).graph_sync_status


class TestOneExtraction:
    def test_a_synchronous_write(self, db, tenant, client, model, writes):
        calls, _ = model
        writes(False)
        r = client.post("/v1/memories", json={"content": "I adopted a dog.", "user_id": "u1"},
                        headers=_head(db, tenant))
        assert r.status_code == 201, r.text
        assert calls == ["I adopted a dog."], "the drain extracted it during the write"
        assert r.json()["status"] == "ready"

    def test_a_synchronous_edit(self, db, tenant, client, model, writes):
        calls, _ = model
        writes(False)
        head = _head(db, tenant)
        mid = client.post("/v1/memories", json={"content": "I adopted a dog.", "user_id": "u1"},
                          headers=head).json()["id"]
        calls.clear()
        r = client.patch(f"/v1/memories/{mid}", json={"content": "I adopted two dogs."},
                         headers=head)
        assert r.status_code == 200, r.text
        assert calls == ["I adopted two dogs."]

    def test_an_import_without_write_async(self, db, tenant, model, writes):
        from korely_agent.services import batch

        calls, _ = model
        writes(False)
        mid, err = batch._import_one(db, tenant.id, {"content": "I adopted a cat.", "user_id": "u1"},
                                     project_id=seeded_project_id(db, tenant.id),
                                     memory_id=uuid.uuid4())
        assert err is None
        assert calls == ["I adopted a cat."]


class TestTheImportLeavesItToTheDrain:
    def test_with_write_async_it_only_writes_and_the_drain_extracts_once(self, db, tenant, model,
                                                                         writes):
        """The default: the import writes, and the drain extracts, four end
        users at a time, dated by the event time on the row."""
        from korely_agent.services import batch

        calls, _ = model
        writes(True)
        uid = uuid.uuid4()
        mid, err = batch._import_one(
            db, tenant.id, {"content": "I adopted a cat.", "user_id": "u1",
                            "timestamp": "2026-03-01T00:00:00+00:00"},
            project_id=seeded_project_id(db, tenant.id), memory_id=uid)
        assert err is None and calls == []
        assert _status(db, uid) == "pending"
        S.process_pending_memories(db, limit=10)
        assert calls == ["I adopted a cat."] and _status(db, uid) == "done"
        assert db.get(AgentMemory, uid).event_time == datetime(2026, 3, 1, tzinfo=timezone.utc)


class TestTheClaim:
    def test_the_row_is_born_claimed(self, db, tenant):
        from korely_agent.services.memory_engine import AgentMemoryEngine

        m = AgentMemoryEngine(db, tenant.id).add("Maria moved to Milan.", end_user_id="u1",
                                                 extracting_now=True)
        db.expire_all()
        row = db.get(AgentMemory, m.id)
        assert row.graph_sync_status == "processing" and row.graph_sync_claimed_at is not None

    def test_a_failed_inline_extraction_goes_back_to_the_queue(self, db, tenant, client, model,
                                                               writes):
        calls, state = model
        writes(False)
        state["fail"] = True
        r = client.post("/v1/memories", json={"content": "I adopted a dog.", "user_id": "u1"},
                        headers=_head(db, tenant))
        assert r.status_code == 201, r.text
        row = db.query(AgentMemory).one()
        db.refresh(row)
        assert (row.graph_sync_status, row.graph_sync_claimed_at) == ("pending", None)
