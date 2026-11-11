"""A memory's `updated_at` is the developer's, and only an edit moves it.

The API returns it, the history of a memory reads an "updated" event off it,
and PATCH checks `expected_updated_at` against it for a stale write. It was
declared with the ORM's automatic `onupdate`, so every write to the row moved
it, and the worker writes the row for its own bookkeeping: the facts drain's
claim ('processing') and its outcome ('done', 'error'), the reaper, `manage
reextract`, the move of a deleted project's rows. Every memory written
asynchronously read as edited a few seconds after it was written, by nobody,
and a caller holding the version it was given got 409 `stale_write` on its
first PATCH once the drain had passed. The vector drain had been careful
(services/vectors.py kept the column by hand); the rest had not.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import seeded_project_id
from tests.conftest import stub_extraction
from korely_agent.db.models import AgentMemory
from korely_agent.services import keys as key_service
from korely_agent.services.facts import service as facts_service
from korely_agent.services.memory_engine import AgentMemoryEngine


# The edit a developer made, long enough ago that no clock can confuse it with
# a write made during the test.
EDITED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


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
def quiet_model(monkeypatch):
    """No embedder, and an extraction that finds nothing: the drain does all of
    its bookkeeping (the claim, then 'done') without a model."""
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
    stub_extraction(monkeypatch, facts_service, lambda *a, **k: [])


def _writes(monkeypatch, asynchronous: bool):
    # Through the real settings, which every door reads (the write is
    # services/memory_write.py's since the audit of 17 Oct 2026).
    from korely_agent.config import get_settings

    monkeypatch.setenv("WRITE_ASYNC", "true" if asynchronous else "false")
    get_settings.cache_clear()


def _an_hour_ago(db, memory_id):
    """The write was an hour ago: the drain now runs an hour after it, so a
    moved value is an hour away from the one the caller holds (written when
    `expected_updated_at` had a one-second tolerance, which hid a value moved
    within the second; since 6 Oct it compares to the microsecond).
    `updated_at` is named, so this line does not move it either."""
    db.query(AgentMemory).filter(AgentMemory.id == memory_id).update(
        {"created_at": AgentMemory.created_at - timedelta(hours=1),
         "updated_at": AgentMemory.updated_at},
        synchronize_session=False)
    db.commit()
    db.expire_all()


def _uuid_of(public_id: str) -> uuid.UUID:
    return uuid.UUID(public_id.removeprefix("mem_"))


def _updated_at(db, memory_id):
    db.expire_all()
    return db.get(AgentMemory, memory_id).updated_at


# ── through the API ─────────────────────────────────────────────────────────


class TestAWriteProcessedInTheBackgroundWasNotEdited:
    def test_an_asynchronous_write_keeps_its_updated_at(
            self, client, head, db, quiet_model, monkeypatch):
        _writes(monkeypatch, asynchronous=True)
        written = client.post("/v1/memories", headers=head, json={
            "content": "Maria lives in Rome.", "user_id": "maria"}).json()
        assert written["status"] == "processing"
        assert written["updated_at"] is None, "nobody has edited it yet"

        out = facts_service.process_pending_memories(db, limit=10)
        assert (out["claimed"], out["processed"]) == (1, 1)

        db.expire_all()
        read = client.get(f"/v1/memories/{written['id']}", headers=head).json()
        assert read["status"] == "ready"
        assert read["updated_at"] == written["updated_at"], (
            "the drain's claim and status moved the developer's updated_at")
        events = [e["event"] for e in client.get(
            f"/v1/memories/{written['id']}/history", headers=head).json()["events"]]
        assert "updated" not in events, (
            "the history shows an edit nobody made")

    def test_nor_does_an_extraction_that_failed(
            self, client, head, db, quiet_model, monkeypatch):
        """The other outcome of the drain: the memory ends 'error'."""
        _writes(monkeypatch, asynchronous=True)

        def refused(*a, **k):
            raise RuntimeError("503 the provider is down")

        stub_extraction(monkeypatch, facts_service, refused)
        written = client.post("/v1/memories", headers=head, json={
            "content": "Maria lives in Rome.", "user_id": "maria"}).json()
        facts_service.process_pending_memories(db, limit=10)

        db.expire_all()
        read = client.get(f"/v1/memories/{written['id']}", headers=head).json()
        assert read["status"] == "error"
        assert read["updated_at"] is None

    def test_a_synchronous_write_answers_with_the_updated_at_it_has(
            self, client, head, db, quiet_model, monkeypatch):
        """WRITE_ASYNC=false: the inline extraction marks the memory 'done'
        before the reply is written, and the reply carried the moved value."""
        _writes(monkeypatch, asynchronous=False)
        written = client.post("/v1/memories", headers=head, json={
            "content": "Maria lives in Rome.", "user_id": "maria"}).json()
        assert written["status"] == "ready"
        assert written["updated_at"] is None
        assert _updated_at(db, _uuid_of(written["id"])) is None


class TestTheVersionACallerHoldsStillWrites:
    def test_expected_updated_at_after_the_drain_passed(
            self, client, head, db, quiet_model, monkeypatch):
        """The caller holds the version it was given, the drain passes, the
        caller edits: that is not a stale write. Then the edit moves the
        version, and the old one is stale."""
        _writes(monkeypatch, asynchronous=True)
        written = client.post("/v1/memories", headers=head, json={
            "content": "Maria lives in Rome.", "user_id": "maria"}).json()
        _an_hour_ago(db, _uuid_of(written["id"]))
        held = client.get(f"/v1/memories/{written['id']}", headers=head).json()
        version = held["updated_at"] or held["created_at"]

        facts_service.process_pending_memories(db, limit=10)
        db.expire_all()

        r = client.patch(f"/v1/memories/{written['id']}", headers=head, json={
            "content": "Maria lives in Milan.", "expected_updated_at": version})
        assert r.status_code == 200, (
            f"the version the caller held was refused after a background pass: {r.text}")
        edited = r.json()
        assert edited["updated_at"] is not None, "an edit is an edit"

        stale = client.patch(f"/v1/memories/{written['id']}", headers=head, json={
            "content": "Maria lives in Turin.", "expected_updated_at": version})
        assert stale.status_code == 409
        assert stale.json()["code"] == "stale_write"

        # The worker processing the edit is not another edit.
        facts_service.process_pending_memories(db, limit=10)
        db.expire_all()
        again = client.patch(f"/v1/memories/{written['id']}", headers=head, json={
            "content": "Maria lives in Turin.",
            "expected_updated_at": edited["updated_at"]})
        assert again.status_code == 200, again.text


# ── every internal writer, one by one ─────────────────────────────────────


@pytest.fixture
def edited(db, tenant):
    """A memory a developer edited on EDITED."""
    engine = AgentMemoryEngine(db, tenant.id, seeded_project_id(db, tenant.id))
    m = engine.add("Maria lives in Rome.", end_user_id="maria")
    db.query(AgentMemory).filter(AgentMemory.id == m.id).update(
        {"updated_at": EDITED}, synchronize_session=False)
    db.commit()
    db.expire_all()
    return m.id


def _set(db, memory_id, **values):
    """Put the row in a state, naming `updated_at` so this line keeps it
    (unless the test gives it a value)."""
    values.setdefault("updated_at", AgentMemory.updated_at)
    db.query(AgentMemory).filter(AgentMemory.id == memory_id).update(
        values, synchronize_session=False)
    db.commit()
    db.expire_all()


class TestNoInternalWriteIsAnEdit:
    def test_the_facts_drain(self, db, edited, quiet_model):
        _set(db, edited, graph_sync_status="pending")
        facts_service.process_pending_memories(db, limit=10)
        db.expire_all()
        assert db.get(AgentMemory, edited).graph_sync_status == "done"
        assert _updated_at(db, edited) == EDITED

    def test_the_reaper_and_the_claim_after_it(self, db, edited, quiet_model):
        _set(db, edited, graph_sync_status="processing",
             graph_sync_claimed_at=datetime.now(timezone.utc) - timedelta(hours=3))
        out = facts_service.process_pending_memories(db, limit=10)
        assert out["claimed"] == 1, "the stranded row was not reclaimed"
        assert _updated_at(db, edited) == EDITED

    def test_reextract(self, db, edited, monkeypatch):
        from korely_agent import manage

        monkeypatch.setattr(manage, "_session", lambda: db)
        _set(db, edited, graph_sync_status="error")
        assert manage.main(["reextract", "--empty"]) == 0
        db.expire_all()
        assert db.get(AgentMemory, edited).graph_sync_status == "pending"
        assert _updated_at(db, edited) == EDITED

    def test_the_vector_drain(self, db, edited, monkeypatch):
        from korely_agent.core.llm import EMBEDDING_DIM
        from korely_agent.services import vectors

        vector = lambda text, **kw: [0.1] * EMBEDDING_DIM  # noqa: E731
        monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
        monkeypatch.setattr("korely_agent.core.llm.embed", vector)
        monkeypatch.setattr("korely_agent.services.memory_engine.embed", vector)
        _set(db, edited, embedding=None, embedding_status="pending", embedding_next_at=None)
        out = vectors.process_pending_vectors(db, limit=10)
        assert out["processed"] >= 1
        assert _updated_at(db, edited) == EDITED

    def test_a_deleted_project_moving_its_memories(self, db, tenant):
        from korely_agent.services import projects as project_service

        default = project_service.get_or_create_default_project(db, tenant.id)
        beta = project_service.create_project(db, tenant_id=tenant.id, name="Beta")
        m = AgentMemoryEngine(db, tenant.id, beta.id).add("Maria lives in Rome.",
                                                          end_user_id="maria")
        _set(db, m.id, updated_at=EDITED)
        project_service.delete_project(db, tenant.id, beta)
        db.expire_all()
        assert db.get(AgentMemory, m.id).project_id == default.id
        assert _updated_at(db, m.id) == EDITED

    def test_but_an_edit_moves_it(self, db, tenant, edited):
        engine = AgentMemoryEngine(db, tenant.id, seeded_project_id(db, tenant.id))
        engine.update(str(edited), "Maria lives in Milan.")
        after = _updated_at(db, edited)
        assert after is not None and after > EDITED
