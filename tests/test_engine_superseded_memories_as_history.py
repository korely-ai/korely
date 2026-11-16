"""A memory whose facts were all superseded, shown as history (memory_core 3.3.0).

Rule (b) of `presentation.memories_for_the_context` (2.3.0) leaves out a
memory every fact of which was closed by a newer one. A WRONG closure by the
judge then hides the memory that holds the evidence too: one error becomes
two. `memories_with_their_state(superseded_as_history=True)` keeps it, flagged, and
`block_line(superseded=True)` puts SUPERSEDED_MEMORY_MARK after its
day. The Cloud reads it from `agent_context._SUPERSEDED_AS_HISTORY` (off, the
block of 3.2.0 byte for byte); the Self-hosted from its twin constant.

No model is called: fake extraction, judge off.
"""
# Port notes: the switch here is korely-agent's twin constant,
# `services.context_experiments.SUPERSEDED_AS_HISTORY`, on since 0.1.23.
#   * The original tests of the cloud's context (off, the superseded memory
#     stays out; on, it comes back marked as history) are already here on this
#     product's block: tests/test_superseded_as_history.py
#     (test_off_the_superseded_memory_is_out, which also asserts the default,
#     on here; test_on_it_stays_marked_as_history). Not duplicated.
#   * The original test that, on with nothing superseded, the block is the
#     one of before is ported on
#     korely-agent's block (`services.context.assemble_context`): "off" is set
#     explicitly, the hosted default being off and this product's on. The facts
#     are written by korely-agent's write with tests.conftest.stub_extraction
#     and the judge off on both of its paths; the question gets a fixed vector
#     (the block searches the memories with it), the search is faked.
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import context as agent_context
from korely_agent.services import context_experiments as experiments
from korely_agent.services.facts import contradiction
from korely_agent.services.facts import service as fsvc
from korely_agent.services.memory_core import presentation as P
from korely_agent.services.memory_core.fact_extractor import ExtractedFact
from korely_agent.services.memory_engine import AgentMemoryEngine
from tests.conftest import seeded_project_id, stub_extraction

DAY = datetime(2023, 5, 8, 10, 0, tzinfo=timezone.utc)


def _rows():
    return [
        {"id": "a", "content": "Ana lives in Rome."},       # every fact superseded
        {"id": "b", "content": "Ana lives in Milan."},      # standing
        {"id": "c", "content": "Hello there."},             # no fact
        {"id": "d", "content": "ana lives in   rome."},     # the same text as "a"
        {"id": "e", "content": "Ana works at Ferrero."},    # superseded
    ]


_STATES = {"a": (1, 0), "b": (1, 1), "c": (0, 0), "d": (1, 0), "e": (2, 0)}


class TestMemoriesWithTheirState:
    def test_off_is_the_rule_of_2_3_0(self):
        kept = P.memories_with_their_state(_rows(), lambda mid: _STATES[mid])
        assert [r["id"] for r, _s in kept] == ["b", "c"]
        assert all(not s for _r, s in kept)
        assert [r["id"] for r in P.memories_for_the_context(_rows(), lambda mid: _STATES[mid])] == ["b", "c"]

    def test_on_keeps_the_superseded_ones_marked(self):
        kept = P.memories_with_their_state(_rows(), lambda mid: _STATES[mid], superseded_as_history=True)
        assert [(r["id"], s) for r, s in kept] == [("a", True), ("b", False), ("c", False), ("e", True)]

    def test_on_a_text_already_shown_does_not_come_back(self):
        # "d" repeats "a" (spaces and case): the first by relevance stays, once.
        kept = P.memories_with_their_state(_rows(), lambda mid: _STATES[mid], superseded_as_history=True)
        assert "d" not in [r["id"] for r, _s in kept]


class TestTheBlockLine:
    def test_whole_with_the_day(self):
        r = {"content": "Ana lives in Rome.", "event_time": DAY}
        assert P.block_line(r, 0) == "- [2023-05-08] Ana lives in Rome."
        assert P.block_line(r, 0, superseded=True) == (
            "- [2023-05-08] (true when said, changed later) Ana lives in Rome.")

    def test_with_the_observation(self):
        r = {"content": "I moved to Rome last year.", "observation": "Ana lives in Rome.", "created_at": DAY}
        assert P.block_line(r, P.WHOLE, superseded=True) == (
            "- [2023-05-08] (true when said, changed later) Ana lives in Rome.")

    def test_without_a_day(self):
        assert P.block_line({"content": "Ana lives in Rome."}, 0, superseded=True) == (
            "- (true when said, changed later) Ana lives in Rome.")

    def test_a_text_that_starts_with_brackets_is_not_the_day(self):
        r = {"content": "[draft] Ana lives in Rome."}
        assert P.block_line(r, 0, superseded=True) == "- (true when said, changed later) [draft] Ana lives in Rome."

    def test_not_marked_is_the_line_as_before(self):
        r = {"content": "Ana lives in Rome.", "event_time": DAY}
        assert P.block_line(r, 0, superseded=False) == P.block_line(r, 0)

    def test_the_mark_does_not_say_it_is_false(self):
        # The lesson of PAST_MARK: "no longer true" read as a negation.
        assert "not" not in P.SUPERSEDED_MEMORY_MARK.split()
        assert "true when said" in P.SUPERSEDED_MEMORY_MARK


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(agent_context, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)
    monkeypatch.setattr(fsvc, "_embed_fact", lambda *a, **k: None)
    monkeypatch.setattr(fsvc, "detect_and_invalidate", lambda *a, **k: [])
    monkeypatch.setattr(contradiction, "detect_and_invalidate_batch", lambda *a, **k: {})


def _memory(db, tenant, text, end_user="ana") -> AgentMemory:
    m = AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id), end_user_id=end_user,
                    content=text, graph_sync_status="pending", event_time=DAY)
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _write(db, tenant, monkeypatch, memory, *facts):
    stub_extraction(monkeypatch, fsvc, lambda *a, **k: list(facts))
    fsvc.extract_and_persist_facts(db, tenant.id, memory=memory)
    db.expire_all()
    return list(db.query(AgentFact).filter(AgentFact.source_memory_id == memory.id))


def _context(db, tenant, monkeypatch, *memories):
    fakes = [{"id": str(x.id), "content": x.content} for x in memories]
    monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, *a, **k: fakes)
    return agent_context.assemble_context(db, tenant.id, query="where does Ana live",
                                          end_user_id="ana", token_budget=800)["context"]


class TestTheCloudContext:
    def test_on_without_superseded_the_block_is_the_one_before(self, db, tenant, monkeypatch):
        a = _memory(db, tenant, "Ana lives in Milan.")
        _write(db, tenant, monkeypatch, a,
                ExtractedFact("Ana", "lives_in", "Milan", 0.9, subject_type="person"))
        b = _memory(db, tenant, "Hello there.")
        monkeypatch.setattr(experiments, "SUPERSEDED_AS_HISTORY", False)
        off = _context(db, tenant, monkeypatch, a, b)
        monkeypatch.setattr(experiments, "SUPERSEDED_AS_HISTORY", True)
        assert _context(db, tenant, monkeypatch, a, b) == off
