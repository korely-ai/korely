"""The observation of a memory (memory_core 2.6.0, 2026-10-02).

The write asks for the facts and, in the same call, one short sentence about
the end user, reading the end user's memories before this one (for pronouns)
and the memory's day (for "yesterday"). /v1/context shows that sentence,
dated, in place of the whole turn: Mem0 and Mastra hand the agent short
memories about the person, and LongMemEval run A lost its evidence in whole
turns. A memory without one reads as before.
"""
from datetime import datetime, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory
from korely_agent.services import context as ctx
from korely_agent.services.facts import service
from korely_agent.services.memory_core import presentation
from korely_agent.services.memory_core.fact_extractor import Extraction
from korely_agent.services.memory_engine import AgentMemoryEngine
from tests.conftest import seeded_project_id


DAY = datetime(2023, 5, 20, 14, 0, tzinfo=timezone.utc)
TURN = ("user: I finally redeemed that $5 coupon on coffee creamer last Sunday, "
        "and honestly the store brand tastes the same.")


def memory(db, tenant, text, *, when=DAY, observation=None, eu="maria"):
    m = AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id), end_user_id=eu,
                    content=text, event_time=when, observation=observation)
    db.add(m)
    db.commit()
    return m


@pytest.fixture
def extraction(monkeypatch):
    """The model's reply, and what the write asked it with."""
    seen = {}

    def fake(content, **kw):
        seen.update(kw, content=content)
        return Extraction([], seen.get("reply"))

    monkeypatch.setattr(service, "extract_facts_and_observation", fake)
    return seen


class TestTheWrite:
    def test_the_observation_is_kept_with_the_memory(self, db, tenant, extraction):
        extraction["reply"] = "On 2023-05-14 the user redeemed a $5 coupon on coffee creamer."
        m = memory(db, tenant, TURN)
        service.extract_and_persist_facts(db, tenant.id, memory=m)
        db.expire_all()
        assert db.get(AgentMemory, m.id).observation == extraction["reply"]

    def test_it_reads_the_memories_before_and_the_day(self, db, tenant, extraction):
        memory(db, tenant, "user: I have a cat called Miso.", when=datetime(2023, 5, 1, tzinfo=timezone.utc))
        memory(db, tenant, "user: Luca's cat is called Pepe.", eu="luca")
        m = memory(db, tenant, "user: She hates the new food.")
        service.extract_and_persist_facts(db, tenant.id, memory=m)
        assert extraction["previous"] == ["user: I have a cat called Miso."]
        assert extraction["date"] == "2023-05-20"

    def test_a_reply_without_one_leaves_none(self, db, tenant, extraction):
        """An edited memory never keeps the sentence of its old text."""
        m = memory(db, tenant, TURN, observation="The user lives in Rome.")
        service.extract_and_persist_facts(db, tenant.id, memory=m)
        db.expire_all()
        assert db.get(AgentMemory, m.id).observation is None


class TestTheContext:
    """The hybrid block (2026-10-02, memory_core's since 2.8.0): the first presentation.WHOLE
    memories whole, the rest through `memory_line`. These tests set the
    first number to 0 where they test the short line itself."""

    @pytest.fixture(autouse=True)
    def _vector(self, monkeypatch):
        monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)

    def context(self, db, tenant, monkeypatch, *memories, budget=2000):
        found = [{"id": str(m.id), "content": m.content} for m in memories]
        self.searched = {}
        def search(eng, q, **k):
            self.searched.update(k)
            return list(found)
        monkeypatch.setattr(AgentMemoryEngine, "search", search)
        return ctx.assemble_context(db, tenant.id, query="what did I redeem?", end_user_id="maria",
                                    token_budget=budget)["context"]

    def test_the_first_of_the_block_stay_whole(self, db, tenant, monkeypatch):
        """Five whole, dated sentences after: details on top, breadth below
        (50/60 against 46 and 36)."""
        mems = [memory(db, tenant, f"user: turn number {i}, with its own details.",
                       observation=f"The user said thing {i}.") for i in range(7)]
        out = self.context(db, tenant, monkeypatch, *mems, budget=4000)
        for i in range(5):
            # Whole, under their day (block_line, memory_core 2.8.0).
            assert f"- [2023-05-20] user: turn number {i}, with its own details." in out
        for i in (5, 6):
            assert f"- [2023-05-20] The user said thing {i}." in out
            assert f"turn number {i}," not in out

    def test_the_search_brings_the_wider_pool(self, db, tenant, monkeypatch):
        self.context(db, tenant, monkeypatch, memory(db, tenant, TURN))
        assert self.searched["limit"] == presentation.POOL == 40

    def test_the_observation_dated_in_place_of_the_turn(self, db, tenant, monkeypatch):
        monkeypatch.setattr(presentation, "WHOLE", 0)
        m = memory(db, tenant, TURN, observation="The user redeemed a $5 coupon on coffee creamer.")
        out = self.context(db, tenant, monkeypatch, m)
        assert "- [2023-05-20] The user redeemed a $5 coupon on coffee creamer." in out
        assert "store brand" not in out

    def test_without_one_the_turn_as_before(self, db, tenant, monkeypatch):
        out = self.context(db, tenant, monkeypatch, memory(db, tenant, TURN))
        assert f"- [2023-05-20] {TURN}" in out

    def test_nothing_about_the_person_the_start_of_the_turn(self, db, tenant, monkeypatch):
        monkeypatch.setattr(presentation, "WHOLE", 0)
        long_turn = "assistant: " + "The Treaty of Westphalia ended the Thirty Years' War. " * 40
        out = self.context(db, tenant, monkeypatch, memory(db, tenant, long_turn, observation=""))
        # memory_core 2.8.0: every line under its day, this one too.
        line = next(l for l in out.split("\n") if l.startswith("- [2023-05-20] assistant:"))
        assert len(line) == 2 + len("[2023-05-20] ") + 1200 + 3 and line.endswith("...")
