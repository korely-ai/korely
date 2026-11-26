"""A backlog of vectors is drained pass after pass, not one pass every
thirty seconds (1.0.40).

The two-process test of 30 Oct 2026: 2,000 memories written in about 20
seconds, most of them stored with no vector (the embedder busy), had their
last vector some 9 minutes later, a pass of `vector_drain_limit` rows every
VECTORS_EVERY seconds, and until then search did not find them. A part's
pass already said `more` on a full pass (`vectors.drain_pass`) and the
worker started the next at once (worker._loop); the memories' and facts'
pass now says it the same way. A pass that missed a vector does not, so a
failing embedder is not asked again before the worker's pause.
"""
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory
from korely_agent.services import vectors
from korely_agent.services.memory_engine import AgentMemoryEngine
from tests.conftest import seeded_project_id


class Embedder:
    def __init__(self):
        self.fail = False

    def __call__(self, text, **kw):
        return None if self.fail else [0.1] * EMBEDDING_DIM


@pytest.fixture
def embedder(monkeypatch):
    e = Embedder()
    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
    monkeypatch.setattr("korely_agent.core.llm.embed", e)
    monkeypatch.setattr("korely_agent.services.memory_engine.embed", e)
    return e


def _waiting(db, tenant, embedder, n):
    """`n` memories stored with no vector, due now."""
    engine = AgentMemoryEngine(db, tenant.id, seeded_project_id(db, tenant.id))
    embedder.fail = True
    for i in range(n):
        engine.add(f"Note number {i} of the import.", end_user_id="maria")
    embedder.fail = False
    db.query(AgentMemory).update(
        {AgentMemory.embedding_next_at: datetime.now(timezone.utc) - timedelta(seconds=1)},
        synchronize_session=False)
    db.commit()


def test_a_full_pass_says_more(db, tenant, embedder):
    _waiting(db, tenant, embedder, 5)
    out = vectors.process_pending_vectors(db, limit=2)
    assert out["processed"] == 2 and out.get("more") is True
    out = vectors.process_pending_vectors(db, limit=2)
    assert out["processed"] == 2 and out.get("more") is True
    out = vectors.process_pending_vectors(db, limit=2)
    assert out["processed"] == 1 and "more" not in out


def test_a_pass_with_room_left_does_not(db, tenant, embedder):
    _waiting(db, tenant, embedder, 3)
    out = vectors.process_pending_vectors(db, limit=10)
    assert out["processed"] == 3 and "more" not in out


def test_a_pass_whose_embedder_fails_does_not(db, tenant, embedder):
    _waiting(db, tenant, embedder, 5)
    embedder.fail = True
    out = vectors.process_pending_vectors(db, limit=2)
    assert out["processed"] == 0 and "more" not in out
