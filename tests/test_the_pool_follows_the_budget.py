"""The search's pool follows the block's budget (12 Oct 2026).

memory_core's POOL (40) fills about 2,000 tokens; a 4,000-token block shown
40 memories stayed half empty, and the evidence of list questions sat past the
40th place. One memory per TOKENS_PER_MEMORY of budget, never fewer than POOL,
at most WIDEST_POOL; past the first whole memories a wider pool is ordered by
maximal marginal relevance (Carbonell and Goldstein, SIGIR 1998;
services/mmr.py). Measured on LoCoMo: +15 of 282 multi-hop questions at 4,000
tokens a person (p 0.011); at 2,000 and below the search is the one of before.
Until 12 Oct it was the bench-only experiment WIDE_POOL.
"""
import math

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory
from korely_agent.services import context as ctx
from korely_agent.services import mmr, vectors
from korely_agent.services.memory_core.presentation import POOL
from korely_agent.services.memory_engine import AgentMemoryEngine
from tests.conftest import seeded_project_id


def vec(**axes):
    """A vector of EMBEDDING_DIM with the given components on the first axes."""
    v = [0.0] * EMBEDDING_DIM
    for k, x in axes.items():
        v[int(k[1:])] = x
    return v


def memory(db, tenant, text, embedding):
    m = AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                    end_user_id="maria", content=text,
                    embedding=embedding, embedding_status=vectors.DONE)
    db.add(m)
    db.commit()
    return m


@pytest.mark.parametrize("budget,pool", [
    (50, POOL), (800, POOL), (2000, POOL), (3000, 60), (4000, 80), (8000, 80),
])
def test_the_pool_for_a_budget(budget, pool):
    assert ctx._pool_for(budget) == pool


def test_the_first_keep_their_rank_and_a_twin_waits(db, tenant):
    q = vec(e0=1.0)
    first = memory(db, tenant, "Maria plans an outdoor wedding.", vec(e0=0.9, e1=math.sqrt(1 - 0.81)))
    twin = memory(db, tenant, "Maria plans a small outdoor wedding.", vec(e0=0.89, e1=math.sqrt(1 - 0.89 ** 2)))
    other = memory(db, tenant, "Maria went to her cousin's wedding.", vec(e0=0.85, e2=math.sqrt(1 - 0.85 ** 2)))
    hits = [{"id": str(m.id)} for m in (first, twin, other)]  # the search's order: by the cosine

    out = mmr.order(db, hits, q, keep_first=1)

    assert [h["id"] for h in out] == [str(first.id), str(other.id), str(twin.id)]
    assert mmr.order(db, hits, q, keep_first=3) == hits


def test_a_memory_with_no_vector_follows(db, tenant):
    q = vec(e0=1.0)
    a = memory(db, tenant, "Maria lives in Rome.", vec(e0=1.0))
    b = memory(db, tenant, "Maria works in Milan.", None)
    c = memory(db, tenant, "Maria runs on Sundays.", vec(e0=0.5, e1=math.sqrt(0.75)))
    hits = [{"id": str(m.id)} for m in (a, b, c)]
    assert [h["id"] for h in mmr.order(db, hits, q, keep_first=1)] == \
        [str(a.id), str(c.id), str(b.id)]


def test_the_search_returns_more_than_50_only_when_asked(db, tenant):
    for i in range(60):
        memory(db, tenant, f"Maria note {i}.", vec(e0=1.0, e1=i / 100))
    engine = AgentMemoryEngine(db, tenant.id, seeded_project_id(db, tenant.id))
    q = vec(e0=1.0)
    assert len(engine.search("Maria", end_user_id="maria", limit=80, query_vector=q)) == 50
    assert len(engine.search("Maria", end_user_id="maria", limit=80, query_vector=q, widest=80)) == 60


def test_the_block_asks_for_what_its_budget_can_show(db, tenant, monkeypatch):
    asked, ordered = [], []
    real = AgentMemoryEngine.search

    def search(self, query, **k):
        asked.append((k["limit"], k.get("widest")))
        return real(self, query, **k)

    real_order = mmr.order

    def order(*a, **k):
        ordered.append(True)
        return real_order(*a, **k)

    monkeypatch.setattr(AgentMemoryEngine, "search", search)
    monkeypatch.setattr(mmr, "order", order)
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: vec(e0=1.0))
    memory(db, tenant, "Maria lives in Rome.", vec(e0=1.0))

    def block(budget):
        return ctx.assemble_context(db, tenant.id, query="Where does Maria live?",
                                    end_user_id="maria", token_budget=budget)

    block(2000)
    assert asked == [(POOL, 50)] and ordered == [], "the search and the order of before"
    block(4000)
    assert asked[-1] == (ctx.WIDEST_POOL, ctx.WIDEST_POOL) and ordered == [True]


def test_the_budgets_bounds_are_written_once():
    """800, 50 and 8000 stood in four places (/v1/context's query, the
    clamp in assemble_context, its default, the MCP tool's default): a bound
    moved in one would answer differently by one door (audit of 17 Oct 2026).
    The REST door and the clamp read services/context's names."""
    import inspect

    from korely_agent.api import context as api_context

    q = inspect.signature(api_context.get_context).parameters["token_budget"].default
    assert q.default == ctx.DEFAULT_TOKEN_BUDGET
    assert [m.ge for m in q.metadata if hasattr(m, "ge")] == [ctx.MIN_TOKEN_BUDGET]
    assert [m.le for m in q.metadata if hasattr(m, "le")] == [ctx.MAX_TOKEN_BUDGET]
    assert str(ctx.DEFAULT_TOKEN_BUDGET) in q.description
    assert inspect.signature(ctx.assemble_context).parameters["token_budget"].default \
        == ctx.DEFAULT_TOKEN_BUDGET
