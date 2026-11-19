"""`agent_id` in /v1/context narrows the memories too, as every other read (2026-10-06).

The block filtered its facts by the agent's namespace and not its memories:
the search took the end user's memories of every agent. A support agent asking
for its context got the sales agent's conversations with the same customer,
under a block that says this is what this agent knows, while POST
/v1/memories/search and GET /v1/memories with the same agent_id answered with
its own only. The hosted product had the same code (code audit of 6 Oct).
"""
import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory
from korely_agent.services import context as ctx
from korely_agent.services import vectors
from tests.conftest import seeded_project_id


SUPPORT = "user: Maria asked for a refund on order 42."
SALES = "user: Maria is negotiating the Enterprise plan for her team."
SHARED = "user: Maria prefers to be written to in Italian."


@pytest.fixture(autouse=True)
def _vector(monkeypatch):
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)


def memory(db, tenant, text, agent):
    m = AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                    end_user_id="maria", agent_id=agent, content=text,
                    embedding=[0.1] * EMBEDDING_DIM, embedding_status=vectors.DONE)
    db.add(m)
    db.commit()
    return m


def context(db, tenant, agent):
    return ctx.assemble_context(db, tenant.id, query="What does Maria need?",
                                end_user_id="maria", agent_id=agent, token_budget=2000)


@pytest.fixture
def three(db, tenant):
    return (memory(db, tenant, SUPPORT, "support"), memory(db, tenant, SALES, "sales"),
            memory(db, tenant, SHARED, None))


def test_an_agents_block_holds_its_own_memories_only(db, tenant, three):
    out = context(db, tenant, "support")
    assert SUPPORT in out["context"]
    assert SALES not in out["context"], "the sales agent's memory reached the support agent"
    assert SHARED not in out["context"], "the agent filter is the one of every other read"


def test_its_sources_name_its_own_memories_only(db, tenant, three):
    support, _sales, _shared = three
    mems = [s for s in context(db, tenant, "support")["sources"] if s.startswith("mem_")]
    assert mems == [f"mem_{support.id.hex}"]


def test_without_an_agent_every_agents_memories_as_before(db, tenant, three):
    out = context(db, tenant, None)["context"]
    assert SUPPORT in out and SALES in out and SHARED in out
