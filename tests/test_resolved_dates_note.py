"""The line on the dates in brackets, under the memories' heading (2026-10-07).

On by default, the same line and place as the hosted product: a
relative time carries its computed date ("next month [June 2023]"), and the
reader is told not to recompute it. Under "## Relevant memories", only when the
block shows memories: the note (`stable`, the cached part) is the one of
before, and a block with no memory lines keeps its room for the facts.
"""
import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory
from korely_agent.services import context as ctx
from korely_agent.services import vectors
from korely_agent.services.memory_engine import AgentMemoryEngine, MemorySearchUnavailable
from tests.conftest import seeded_project_id



@pytest.fixture
def memory(db, tenant, monkeypatch):
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)
    db.add(AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                       end_user_id="maria", content="user: Maria moves to Rome next month.",
                       embedding=[0.1] * EMBEDDING_DIM, embedding_status=vectors.DONE))
    db.commit()


def block(db, tenant, budget=2000):
    return ctx.assemble_context(db, tenant.id, query="When does Maria move?",
                                end_user_id="maria", token_budget=budget)


def test_on_under_the_memories_heading(db, tenant, memory):
    assert ctx._RESOLVED_DATES_NOTE is True
    out = block(db, tenant)
    assert ctx.MEMORIES_HEADING + "\n" + ctx.RESOLVED_DATES_LINE + "\n- " in out["volatile"]
    assert out["stable"] == ctx._READER_GUIDANCE


def test_the_line_is_the_only_difference(db, tenant, memory, monkeypatch):
    on = block(db, tenant)
    monkeypatch.setattr(ctx, "_RESOLVED_DATES_NOTE", False)
    off = block(db, tenant)
    assert on["stable"] == off["stable"] and on["sources"] == off["sources"]
    assert on["volatile"].replace("\n" + ctx.RESOLVED_DATES_LINE, "", 1) == off["volatile"]


def test_no_memories_no_line_and_the_facts_keep_their_room(db, tenant, monkeypatch):
    from korely_agent.services.facts.service import write_fact_direct

    monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)
    write_fact_direct(db, tenant.id, subject="maria", predicate="lives_in", obj="Rome",
                      end_user_id="maria", project_id=seeded_project_id(db, tenant.id))
    db.commit()

    def down(self, *a, **k):
        raise MemorySearchUnavailable("down")

    monkeypatch.setattr(AgentMemoryEngine, "search", down)
    out = block(db, tenant, budget=400)
    assert ctx.RESOLVED_DATES_LINE not in out["context"]
    assert "Rome" in out["context"]
