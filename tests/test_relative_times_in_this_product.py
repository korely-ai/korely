"""memory_core 2.9.0 wired into this product (2026-10-06): the relative times
dated by code.

The extraction's `times` (the model's relative times, dated from the memory's
day by `relative_time.annotate`) is stored on the memory, always assigned like
the observation, and /v1/context passes it to `block_line`, which puts
the date next to the words of a memory shown whole: "yesterday [2023-05-07]".

The other rule of 2.9.0, the facts the assistant alone said closing nothing,
was measured without gain (127 against 129 of 152, p 0.80) and removed from
this adapter on 2026-10-06, with its tests.
"""
from datetime import datetime, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory
from korely_agent.services import context as ctx
from korely_agent.services.facts import service
from korely_agent.services.memory_core import presentation, relative_time
from korely_agent.services.memory_core.fact_extractor import Extraction
from korely_agent.services.memory_engine import AgentMemoryEngine
from tests.conftest import seeded_project_id


MAY_8 = datetime(2023, 5, 8, 13, 56, tzinfo=timezone.utc)
TURN = "user: I went to a support group yesterday."
# "Yesterday" as memory_core 3.3.0 asks for it: last, unit day. Before with a
# count of one and no anchor is refused (the one-day guard, TIDES 2005 4.3.2).
YESTERDAY = [{"text": "yesterday", "direction": "last", "unit": "day"}]


def memory(db, tenant, text, *, relative_times=None, eu="caroline"):
    m = AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id), end_user_id=eu,
                    content=text, event_time=MAY_8, relative_times=relative_times)
    db.add(m)
    db.commit()
    return m


def dated(text):
    value, problems = relative_time.annotate(YESTERDAY, "2023-05-08", text)
    assert problems == []
    return value


class TestTheWriteStoresTheTimes:
    def test_the_extractions_times_land_on_the_memory(self, db, tenant, monkeypatch):
        times = dated(TURN)
        monkeypatch.setattr(service, "extract_facts_and_observation",
                            lambda content, **kw: Extraction([], "The user went to a group.", times))
        m = memory(db, tenant, TURN)
        service.extract_and_persist_facts(db, tenant.id, memory=m)
        db.expire_all()
        assert db.get(AgentMemory, m.id).relative_times == times

    def test_always_assigned_so_an_edit_never_keeps_old_times(self, db, tenant, monkeypatch):
        monkeypatch.setattr(service, "extract_facts_and_observation",
                            lambda content, **kw: Extraction([], "The user went to a group.", None))
        m = memory(db, tenant, TURN, relative_times=dated(TURN))
        service.extract_and_persist_facts(db, tenant.id, memory=m)
        db.expire_all()
        assert db.get(AgentMemory, m.id).relative_times is None


class TestTheBlockShowsTheDates:
    @pytest.fixture(autouse=True)
    def _vector(self, monkeypatch):
        monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)

    def test_the_date_next_to_the_words_of_a_whole_memory(self, db, tenant, monkeypatch):
        m = memory(db, tenant, TURN, relative_times=dated(TURN))
        monkeypatch.setattr(AgentMemoryEngine, "search",
                            lambda eng, q, **k: [{"id": str(m.id), "content": m.content}])
        out = ctx.assemble_context(db, tenant.id, query="When did Caroline go to the group?",
                                   end_user_id="caroline", token_budget=2000)["context"]
        expected = presentation.block_line(
            {"content": TURN, "observation": None, "event_time": MAY_8, "created_at": None,
             "relative_times": m.relative_times}, 0)
        assert expected == "- [2023-05-08] user: I went to a support group yesterday [2023-05-07]."
        assert expected in out
