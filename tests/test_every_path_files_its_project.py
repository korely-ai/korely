"""Every path that calls the embedder for one project files its cost row
there (1.0.41): a search of the memories, a context read, and the worker's
vector of a memory. The rows are written by the usage's own session, as in
production (no session patched), and read back from the test's.

Each test fails if its call site stops passing the project: the row would
land under "no project", and the report's By project would lose it.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentLlmUsage, AgentMemory
from korely_agent.services import llm_usage, projects

VEC = [0.1] * EMBEDDING_DIM


@pytest.fixture
def embedder(monkeypatch):
    """An embedder that records its call as the real ones do (core/llm,
    `llm_usage.record` under the caller's scope) and answers a vector."""
    calls = []

    def embed(text, **kw):
        calls.append(text)
        llm_usage.record("gemini", "embedding-model", 3, 0, stage=llm_usage.EMBEDDING)
        return list(VEC)

    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
    monkeypatch.setattr("korely_agent.core.llm.embed", embed)
    monkeypatch.setattr("korely_agent.services.memory_engine.embed", embed)
    monkeypatch.setattr("korely_agent.services.context.embed", embed)
    return calls


def _project(db, tenant_id, name):
    projects.get_or_create_default_project(db, tenant_id)
    p = projects.create_project(db, tenant_id=tenant_id, name=name)
    db.commit()
    return p.id


def _filed(db, tenant_id):
    db.expire_all()
    return [r.project_id for r in
            db.query(AgentLlmUsage).filter(AgentLlmUsage.tenant_id == tenant_id).all()]


def test_a_search_of_one_project_files_its_question_s_vector_there(db, tenant, embedder):
    from korely_agent.services.memory_engine import AgentMemoryEngine

    tid = tenant.id
    pid = _project(db, tid, "Search")
    AgentMemoryEngine(db, tid, pid).question_vector("where does Maria live?")
    assert embedder and _filed(db, tid) == [pid]


def test_a_context_read_of_one_project_files_its_question_s_vector_there(db, tenant, embedder):
    from korely_agent.services import context

    tid = tenant.id
    pid = _project(db, tid, "Context")
    context._retrieve(db, tid, query="where does Maria live?", end_user_id=None, agent_id=None,
                      include_facts=False, project_id=pid)
    assert embedder and _filed(db, tid) == [pid]


def test_the_worker_files_a_memory_s_vector_in_the_memory_s_project(db, tenant, embedder):
    from korely_agent.services import vectors

    tid = tenant.id
    pid = _project(db, tid, "Vectors")
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tid, project_id=pid, end_user_id="u1",
                    agent_id="bot", content="Maria lives in Rome.",
                    embedding_status="pending", embedding_attempts=0,
                    embedding_next_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    db.add(m)
    db.commit()
    vectors.process_pending_vectors(db, limit=10)
    assert embedder and set(_filed(db, tid)) == {pid}
