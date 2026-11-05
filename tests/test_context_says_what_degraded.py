"""/v1/context says which part of it came back degraded (2026-09-28).

The block is facts ranked by the question, then memories found by the
question. Both need the question's vector. When the memories could not be
searched the reply said so (`degraded`, and a NOTE in the block). When the
facts could not be ranked it said nothing: they fell back to the most
recently confirmed ones, `degraded` stayed false, and a block ranked by date
read like one ranked by the question, so the model reading it took "no
relevant fact here" for an answer about the user.

Since 2026-10-01 the question is embedded once for both parts (BUG-299 on the
hosted product), so a missing vector degrades both; the facts alone degrade
when their ranking query fails, and that failure is rolled back to a
savepoint, never with the whole transaction (BUG-300).
"""
import pytest
from fastapi.testclient import TestClient

from korely_agent.core.llm import EMBEDDING_DIM, TASK_QUERY
from korely_agent.db.models import AgentFact
from korely_agent.services import context as ctx
from korely_agent.services import keys as key_service
from korely_agent.services import memory_engine
from korely_agent.services.context import assemble_context
from korely_agent.services.facts.service import write_fact_direct
from korely_agent.services.memory_engine import AgentMemoryEngine, MemorySearchUnavailable
from tests.conftest import seeded_project_id


VEC = [0.1] * EMBEDDING_DIM


@pytest.fixture
def query_vector(monkeypatch):
    """The question's vector: there (`VEC`), missing (None), or of another
    size, which the facts ranking query fails on in Postgres."""
    def set_(vector):
        monkeypatch.setattr(ctx, "embed", lambda *a, **k: vector)
    return set_


@pytest.fixture
def memories_down(monkeypatch):
    def _down(self, *a, **k):
        raise MemorySearchUnavailable("down")
    monkeypatch.setattr(AgentMemoryEngine, "search", _down)


@pytest.fixture
def no_memories(monkeypatch):
    monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, q, **k: [])


@pytest.fixture
def a_fact(db, tenant):
    write_fact_direct(db, tenant.id, subject="Maria", predicate="lives_in",
                      obj="Milan", end_user_id="u1",
                      project_id=seeded_project_id(db, tenant.id))
    db.commit()
    # A vector of its own, so the ranking by the question runs for real (and
    # a question's vector of another size fails on it, as in Postgres).
    db.query(AgentFact).update({AgentFact.embedding: VEC, AgentFact.embedding_status: "done"})
    db.commit()


def context(db, tenant):
    return assemble_context(db, tenant.id, query="where does Maria live",
                            end_user_id="u1", token_budget=800,
                            project_id=seeded_project_id(db, tenant.id))


class TestWhichPart:
    def test_facts_that_could_not_be_ranked_are_reported(
            self, db, tenant, a_fact, query_vector, no_memories):
        query_vector([0.1] * 3)
        out = context(db, tenant)
        assert out["degraded"] is True, "the facts fell back to recency in silence"
        assert out["degraded_parts"] == ["facts"]
        assert "could not be ranked against the question" in out["context"]
        assert "Milan" in out["context"], "the facts it still had were thrown away"

    def test_memories_that_could_not_be_searched_are_reported(
            self, db, tenant, a_fact, query_vector, memories_down):
        query_vector(VEC)
        out = context(db, tenant)
        assert (out["degraded"], out["degraded_parts"]) == (True, ["memories"])
        assert "could not be retrieved" in out["context"]
        assert "could not be ranked" not in out["context"]

    def test_both(self, db, tenant, a_fact, query_vector):
        """What a provider that is down does: one note naming both."""
        query_vector(None)
        out = context(db, tenant)
        assert out["degraded_parts"] == ["facts", "memories"]
        assert out["context"].count("NOTE:") == 1
        assert "could not be ranked" in out["context"]
        assert "could not be retrieved" in out["context"]

    def test_healthy_is_not_degraded(self, db, tenant, a_fact, query_vector):
        """The control: the flag measures the failure, not the stubs. The
        memory search is the real one, with the vector it was handed."""
        query_vector(VEC)
        out = context(db, tenant)
        assert (out["degraded"], out["degraded_parts"]) == (False, [])
        assert "NOTE:" not in out["context"]

    def test_no_facts_to_rank_is_nothing_lost(self, db, tenant, query_vector):
        """A recency fallback that returns nothing has lost nothing; the
        memories, which need the same vector, are reported."""
        query_vector(None)
        out = context(db, tenant)
        assert (out["degraded"], out["degraded_parts"]) == (True, ["memories"])


class TestOneVectorPerRequest:
    """BUG-299 on the hosted product (2026-10-01): the question was embedded
    twice, once for the facts and once for the memory search."""

    def test_one_call(self, db, tenant, a_fact, monkeypatch):
        calls = []

        def counted(text, task=None):
            calls.append((text, task))
            return VEC

        monkeypatch.setattr(ctx, "embed", counted)
        monkeypatch.setattr(memory_engine, "embed", counted)
        out = assemble_context(db, tenant.id, query="  where does Maria live ", end_user_id="u1",
                               project_id=seeded_project_id(db, tenant.id))
        assert calls == [("where does Maria live", TASK_QUERY)]
        assert out["degraded_parts"] == []

    def test_a_missing_vector_is_not_asked_for_again(self, db, tenant, a_fact, monkeypatch):
        searched = []
        monkeypatch.setattr(ctx, "embed", lambda *a, **k: None)
        monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, q, **k: searched.append(q) or [])
        out = context(db, tenant)
        assert out["degraded_parts"] == ["facts", "memories"]
        assert searched == []  # no second call through the memory search


class TestAFailedRankingIsASavepoint:
    """BUG-300 on the hosted product (2026-10-01): a failed statement aborts a
    Postgres transaction. The ranking query runs in a savepoint; this used to
    roll back the whole transaction instead."""

    def test_what_the_session_held_survives(self, db, tenant, a_fact, query_vector, no_memories):
        pending = AgentFact(tenant_id=tenant.id, end_user_id="u1", subject_label="Luca",
                            predicate_raw="likes", predicate_canonical="likes",
                            object_label="tea", project_id=seeded_project_id(db, tenant.id))
        db.add(pending)
        db.flush()  # in the transaction, not committed
        query_vector([0.1] * 3)  # the ranking query fails on the vector's size
        out = context(db, tenant)
        assert out["degraded_parts"] == ["facts"]
        assert "Luca likes tea" in out["context"]
        assert db.query(AgentFact).filter(AgentFact.id == pending.id).count() == 1


def test_the_facts_index_is_asked_for_the_whole_pool(db, tenant, a_fact):
    """BUG-320 on the hosted product (2026-10-01): the facts' HNSW index holds
    every tenant's facts and the wall applies after the scan, so the ranking
    lets it bring the pool (three times the limit, at least 30), as the
    memory search does."""
    from sqlalchemy import text as sql

    ctx._rank_facts(db, tenant.id, "u1", None, "where does Maria live", 20, query_vector=VEC)
    assert db.execute(sql("SELECT current_setting('hnsw.ef_search', true)")).scalar() == "60"
    assert db.execute(sql("SELECT current_setting('hnsw.iterative_scan', true)")).scalar() == "strict_order"
    db.rollback()


def test_the_api_carries_it(db, tenant, a_fact, query_vector):
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    query_vector(None)
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="t")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        r = c.get("/v1/context", params={"query": "where does Maria live", "user_id": "u1"},
                  headers={"Authorization": f"Bearer {full}"})
    app.dependency_overrides.clear()
    assert r.status_code == 200, r.text
    assert (r.json()["degraded"], r.json()["degraded_parts"]) == (True, ["facts", "memories"])
