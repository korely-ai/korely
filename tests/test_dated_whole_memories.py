"""The day on the memories shown whole (2026-10-05; memory_core's rule since 2.8.0).

The hybrid block shows the first presentation.WHOLE memories whole and the
rest as dated observations. The whole ones carried no day, so a reader met "I
went to a support group yesterday" with only the current date to count from:
on the LoCoMo trial (conversation 26) the reader counted "yesterday" and "last
week" from the last session's day, 26 of 37 temporal questions wrong; with the
day, 107 to 121 of 152. memory_core 2.8.0 (`block_line`) puts the day the
memory was said, read from the store, in front of every line of the block.
"""
from datetime import datetime, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory
from korely_agent.services import context as ctx
from korely_agent.services.memory_core import presentation
from korely_agent.services.memory_engine import AgentMemoryEngine
from tests.conftest import seeded_project_id


MAY_8 = datetime(2023, 5, 8, 13, 56, tzinfo=timezone.utc)
TURN = "user: Caroline: I went to a LGBTQ support group yesterday and it was so powerful."


def memory(db, tenant, text, *, when=MAY_8, observation=None, eu="caroline"):
    m = AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id), end_user_id=eu,
                    content=text, event_time=when, observation=observation)
    db.add(m)
    db.commit()
    return m


@pytest.fixture(autouse=True)
def _vector(monkeypatch):
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)


def block(db, tenant, monkeypatch, *memories, budget=2000):
    found = [{"id": str(m.id), "content": m.content} for m in memories]
    monkeypatch.setattr(AgentMemoryEngine, "search", lambda eng, q, **k: list(found))
    return ctx.assemble_context(db, tenant.id, query="When did Caroline go to the support group?",
                                end_user_id="caroline", token_budget=budget)["context"]


class TestTheDayOnTheWholeOnes:
    def test_a_whole_memory_carries_the_day_it_was_said(self, db, tenant, monkeypatch):
        out = block(db, tenant, monkeypatch, memory(db, tenant, TURN))
        assert f"- [2023-05-08] {TURN}" in out

    def test_the_creation_time_when_the_write_gave_no_event_time(self, db, tenant, monkeypatch):
        m = memory(db, tenant, TURN, when=None)
        day = m.created_at.astimezone(timezone.utc).date().isoformat()
        out = block(db, tenant, monkeypatch, m)
        assert f"- [{day}] {TURN}" in out

    def test_the_day_is_the_stores_whatever_the_text_says(self, db, tenant, monkeypatch):
        """No special case for a text that carries a date of its own: the day
        comes from the write's timestamp, the same for every memory (the
        rule of 2026-10-05, no test-shaped exceptions)."""
        stamped = "[2023/02/22 (Wed) 08:13] user: I just bought a new road bike."
        out = block(db, tenant, monkeypatch, memory(db, tenant, stamped))
        assert f"- [2023-05-08] {stamped}" in out

    def test_the_observations_below_keep_their_one_day(self, db, tenant, monkeypatch):
        """The short lines already carry the day: never two."""
        monkeypatch.setattr(presentation, "WHOLE", 1)
        first = memory(db, tenant, TURN)
        second = memory(db, tenant, "user: Caroline: I signed up for a pottery class.",
                        observation="The user signed up for a pottery class.")
        out = block(db, tenant, monkeypatch, first, second, budget=4000)
        assert f"- [2023-05-08] {TURN}" in out
        assert "- [2023-05-08] The user signed up for a pottery class." in out
        assert "[2023-05-08] [2023-05-08]" not in out
