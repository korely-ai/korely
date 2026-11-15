"""Every list that pages or fills a block has a total order (2026-10-06).

Rows written together share their time: the facts of one memory carry the
moment it was said (`last_confirmed_at`, `valid_from`), the memories of a
batch import the moment of its transaction. Ordered by that time alone,
Postgres may return tied rows in any order, and in another on the next call:
a client paging with `offset` repeats some rows and never sees others, and
the same /v1/context request gives two different blocks. Found by the golden
master of the assemble_context refactor (the Bussola's lane 3): two runs of
the same code on the same store, one block apart. The id breaks every tie.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import context as ctx
from korely_agent.services.facts.service import retrieve_agent_facts
from korely_agent.services.memory_engine import AgentMemoryEngine
from korely_agent.services.sentinels import EVERY_PROJECT


SAID = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def _facts(db, tenant, n, **kw):
    ids = []
    for i in range(n):
        f = AgentFact(tenant_id=tenant.id, end_user_id="maria", subject_label="maria",
                      subject_type="self", predicate_raw="likes", predicate_canonical="likes",
                      object_label=f"fruit {i}", object_is_literal=True, confidence=0.9,
                      valid_from=SAID, last_confirmed_at=SAID, **kw)
        db.add(f)
        db.flush()
        ids.append(f.id)
    db.commit()
    return ids


def test_memories_written_together_page_once_each(db, tenant):
    ids = []
    for i in range(7):
        m = AgentMemory(tenant_id=tenant.id, end_user_id="maria", content=f"turn {i}",
                        created_at=SAID, graph_sync_status="done")
        db.add(m)
        db.flush()
        ids.append(str(m.id))
    db.commit()
    engine = AgentMemoryEngine(db, tenant.id)
    seen = []
    for offset in range(0, 7, 2):
        page, total = engine.get_all(end_user_id="maria", limit=2, offset=offset)
        seen += [r["id"] for r in page]
    assert total == 7
    assert seen == sorted(ids, reverse=True), "tied memories come newest id first, once each"


@pytest.mark.parametrize("as_of", [None, datetime(2026, 9, 2, tzinfo=timezone.utc)])
def test_facts_said_together_page_once_each(db, tenant, as_of):
    ids = _facts(db, tenant, 7)
    seen = []
    for offset in range(0, 7, 2):
        rows, total = retrieve_agent_facts(db, tenant.id, end_user_id="maria", as_of=as_of,
                                           limit=2, offset=offset, project_id=EVERY_PROJECT)
        seen += [f.id for f in rows]
    assert total == 7
    assert seen == sorted(ids)


def test_the_block_orders_tied_facts_by_id(db, tenant, monkeypatch):
    """No fact holds a vector: the facts for the question are the most
    recently confirmed, all confirmed in the same moment."""
    ids = _facts(db, tenant, 5)
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)
    monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, q, **k: [])
    out = ctx.assemble_context(db, tenant.id, query="what does maria like?", end_user_id="maria",
                               token_budget=2000)
    from korely_agent.services.ids import fact_public_id

    assert out["sources"] == [fact_public_id(i) for i in sorted(ids)]


def test_the_block_orders_facts_at_one_distance_by_id(db, tenant, monkeypatch):
    """Five facts with one vector, the question's: one distance for all."""
    v = [0.0] * EMBEDDING_DIM
    v[0] = 1.0
    ids = _facts(db, tenant, 5, embedding=v, embedding_status="done")
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: list(v))
    monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, q, **k: [])
    out = ctx.assemble_context(db, tenant.id, query="what does maria like?", end_user_id="maria",
                               token_budget=2000)
    from korely_agent.services.ids import fact_public_id

    assert out["sources"] == [fact_public_id(i) for i in sorted(ids)]
