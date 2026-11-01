"""`min_score` on a memory search (2026-10-07, for both products).

POST /v1/memories/search and the MCP search tool take an optional floor: a
memory whose `score` (the cosine, as the response shows it, rounded to four
places) is below it is left out, before the two rankings are fused (Mem0's
rule). Absent, the search is the one of before. 0 to 1, a number: anything else
is refused. Measured value in the docs: 0.20 (the floor).
"""
import math
import uuid

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory
from korely_agent.services import keys as key_service
from korely_agent.services import limits, vectors
from korely_agent.services.memory_engine import AgentMemoryEngine
from tests.conftest import seeded_project_id



def vec(**axes):
    v = [0.0] * EMBEDDING_DIM
    for k, x in axes.items():
        v[int(k[1:])] = x
    return v


Q = vec(e0=1.0)


def memory(db, tenant, text, cos):
    """A memory whose cosine to Q is `cos`."""
    m = AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                    end_user_id="maria", content=text,
                    embedding=vec(e0=cos, e1=math.sqrt(1 - cos * cos)), embedding_status=vectors.DONE)
    db.add(m)
    db.commit()
    return m


@pytest.fixture
def three(db, tenant):
    return (memory(db, tenant, "Maria lives in Rome.", 0.9),
            memory(db, tenant, "Maria visited Turin once.", 0.2),
            memory(db, tenant, "The weather in Oslo.", 0.1))


def found(db, tenant, **k):
    engine = AgentMemoryEngine(db, tenant.id, seeded_project_id(db, tenant.id))
    return [h["content"] for h in engine.search("Where does Maria live?", end_user_id="maria",
                                                query_vector=Q, limit=10, **k)]


def test_absent_every_nearest_memory_as_before(db, tenant, three):
    assert len(found(db, tenant)) == 3


def test_a_score_shown_as_the_floor_stays(db, tenant, three):
    """1 - 0.8 is 0.19999999999999996 in floating point: the floor reads the
    score as the response shows it, 0.2."""
    assert found(db, tenant, min_score=0.2) == ["Maria lives in Rome.", "Maria visited Turin once."]
    assert found(db, tenant, min_score=0.5) == ["Maria lives in Rome."]
    assert found(db, tenant, min_score=0.95) == []


@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient
    from korely_agent import config
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setattr("korely_agent.services.memory_engine.embed", lambda *a, **k: Q)
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
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}",
                                            scopes=["memories:read", "memories:write"])
    return {"Authorization": f"Bearer {full}"}


def search(client, head, **extra):
    return client.post("/v1/memories/search", headers=head,
                       json={"query": "Where does Maria live?", "user_id": "maria", **extra})


def test_rest_takes_it_and_shows_the_kept_scores(client, head, three):
    r = search(client, head, min_score=0.2)
    assert r.status_code == 200, r.text
    assert [h["score"] for h in r.json()["results"]] == [0.9, 0.2]
    assert len(search(client, head).json()["results"]) == 3


@pytest.mark.parametrize("bad", [-0.1, 1.5, "0.5", True, "high"])
def test_rest_refuses_what_is_not_a_score(client, head, three, bad):
    r = search(client, head, min_score=bad)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "invalid_request"


def test_the_bounds_are_shared_by_rest_and_mcp():
    from korely_agent.api.memories import SearchBody

    field = SearchBody.model_fields["min_score"]
    bounds = {type(m).__name__: getattr(m, "ge", getattr(m, "le", None)) for m in field.metadata
              if type(m).__name__ in ("Ge", "Le")}
    assert bounds == {"Ge": limits.MIN_SCORE_MIN, "Le": limits.MIN_SCORE_MAX}
    assert limits.min_score_problem(None) is None and limits.min_score_problem(0.2) is None
    for bad in (-0.1, 1.01, True, "0.2"):
        assert limits.min_score_problem(bad)
