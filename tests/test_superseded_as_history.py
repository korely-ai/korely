"""SUPERSEDED_AS_HISTORY: a memory whose facts were all superseded, kept as history (2026-10-08).

context_experiments.SUPERSEDED_AS_HISTORY, bench-only until measured
(memory_core 3.3.0 `memories_with_their_state`; the hosted twin is
agent_context._SUPERSEDED_AS_HISTORY). Off, the rule of before: such a memory
is out of the block. On, it stays, its line marked after its day.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import context as ctx
from korely_agent.services import context_experiments as experiments
from korely_agent.services import vectors
from korely_agent.services.memory_core.presentation import SUPERSEDED_MEMORY_MARK
from tests.conftest import seeded_project_id

NOW = datetime.now(timezone.utc)


@pytest.fixture
def store(db, tenant, monkeypatch):
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)
    project = seeded_project_id(db, tenant.id)

    def memory(text, days_ago):
        m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project, end_user_id="maria",
                        content=text, embedding=[0.1] * EMBEDDING_DIM, embedding_status=vectors.DONE,
                        graph_sync_status="done", event_time=NOW - timedelta(days=days_ago))
        db.add(m)
        db.flush()
        return m

    def fact(m, obj, closed_by=None):
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project, end_user_id="maria",
                      subject_label="maria", predicate_raw="on_diet", predicate_canonical="on_diet",
                      object_label=obj, confidence=0.9, valid_from=NOW - timedelta(days=100),
                      source_memory_id=m.id, source_memory_ids=[str(m.id)])
        if closed_by is not None:
            f.invalid_at, f.invalidated_by_id = NOW - timedelta(days=5), closed_by.id
        db.add(f)
        db.flush()
        return f

    old = memory("user: I am on a keto diet, here is my dinner [image: a plate of salmon].", 30)
    new = memory("user: I stopped the keto diet.", 5)
    newer = fact(new, "no diet")
    fact(old, "keto", closed_by=newer)
    db.commit()


def block(db, tenant):
    return ctx.assemble_context(db, tenant.id, query="What did Maria eat for dinner?",
                                end_user_id="maria", token_budget=2000)["volatile"]


def test_off_the_superseded_memory_is_out(db, tenant, store, monkeypatch):
    assert experiments.SUPERSEDED_AS_HISTORY is True, "on since 0.1.23"
    monkeypatch.setattr(experiments, "SUPERSEDED_AS_HISTORY", False)
    out = block(db, tenant)
    assert "plate of salmon" not in out
    assert "stopped the keto diet" in out


def test_on_it_stays_marked_as_history(db, tenant, store, monkeypatch):
    monkeypatch.setattr(experiments, "SUPERSEDED_AS_HISTORY", True)
    out = block(db, tenant)
    line = next(l for l in out.splitlines() if "plate of salmon" in l)
    assert SUPERSEDED_MEMORY_MARK.strip() in line
    assert SUPERSEDED_MEMORY_MARK.strip() not in next(l for l in out.splitlines()
                                                            if "stopped the keto diet" in l)
