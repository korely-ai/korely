"""A memory of something that happened stays in the context (memory_core 2.5.1).

Found and measured on a LongMemEval run (2026-10-01): a fact
stated in the past tense is stored born closed (`writing`, it is history),
and `store.state_of_the_source` counted it as no longer true, so rule (b) of
`presentation.memories_for_the_context` dropped "I redeemed a $5 coupon on
coffee creamer last Sunday" from the context even when the search put it
first. A fact closed by a newer fact stays superseded and its memory stays out.

No model is called: fake extraction, judge off.
"""
# Notes: four of the five tests first written for this rule are already here,
# written on korely-agent's tables, and are not duplicated:
#   * TestStateOfTheSource::test_a_past_fact_is_history_and_stays_standing ->
#     tests/test_memory_core_history_in_context.py::TestHistoryStands::
#     test_a_past_fact_closed_at_birth_is_standing;
#   * TestStateOfTheSource::test_a_fact_superseded_by_a_new_one_stays_superseded ->
#     ...::TestWhatIsSupersededStaysOut::test_replaced_by_a_newer_fact;
#   * TestStateOfTheSource::test_a_fact_closed_without_being_said_in_the_past_is_not_history ->
#     ...::TestWhatIsSupersededStaysOut::test_closed_without_the_past_tense;
#   * TestStatesOfTheSourcesInBlock::test_same_as_state_of_the_source_case_by_case ->
#     tests/test_source_states.py::TestTheSameAnswerAsStateOfTheSource::test_every_kind_of_memory.
# TestTheContext is ported: it reads the block korely-agent's /v1/context
# assembles (`services.context.assemble_context`, which states the memories in
# one query), where the existing test reads `memories_for_the_context` with
# `state_of_the_source` directly. The facts are written by korely-agent's write
# (`facts.service.extract_and_persist_facts`) with the extraction faked by
# tests.conftest.stub_extraction and the judge off on both of its paths.
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import context as agent_context
from korely_agent.services.facts import contradiction
from korely_agent.services.facts import service as fsvc
from korely_agent.services.memory_core.fact_extractor import ExtractedFact
from korely_agent.services.memory_engine import AgentMemoryEngine
from tests.conftest import seeded_project_id, stub_extraction



@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    # The block searches the memories with the question's vector (a missing one
    # is MemorySearchUnavailable here): a fixed one, as
    # tests/test_memory_core_observation.py gives it; the search is faked below.
    monkeypatch.setattr(agent_context, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)
    monkeypatch.setattr(fsvc, "_embed_fact", lambda *a, **k: None)
    monkeypatch.setattr(fsvc, "detect_and_invalidate", lambda *a, **k: [])
    monkeypatch.setattr(contradiction, "detect_and_invalidate_batch", lambda *a, **k: {})


def _memory(db, tenant, text, end_user="ana") -> AgentMemory:
    m = AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id), end_user_id=end_user,
                    content=text, graph_sync_status="pending")
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _write(db, tenant, monkeypatch, memory, *facts):
    stub_extraction(monkeypatch, fsvc, lambda *a, **k: list(facts))
    fsvc.extract_and_persist_facts(db, tenant.id, memory=memory)
    db.expire_all()
    return list(db.query(AgentFact).filter(AgentFact.source_memory_id == memory.id))


class TestTheContext:
    # Written for the hosted product's default, superseded memories left out
    # of the block; korely-agent keeps them as history since 0.1.23
    # (context_experiments.SUPERSEDED_AS_HISTORY, tested on in
    # test_superseded_as_history.py), so this runs with it off, as written.
    def test_the_memory_of_something_that_happened_enters_the_context(self, db, tenant, monkeypatch):
        from korely_agent.services import context_experiments

        monkeypatch.setattr(context_experiments, "SUPERSEDED_AS_HISTORY", False)
        happened = _memory(db, tenant, "I redeemed a $5 coupon on coffee creamer last Sunday.")
        _write(db, tenant, monkeypatch, happened,
                ExtractedFact("Ana", "redeemed", "a $5 coupon", 0.9, subject_type="person", tense="past"))
        refuted = _memory(db, tenant, "Ana lives in Rome.")
        (rome,) = _write(db, tenant, monkeypatch, refuted,
                          ExtractedFact("Ana", "lives_in", "Rome", 0.9, subject_type="person"))
        milan = _memory(db, tenant, "Ana lives in Milan.")
        (true_one,) = _write(db, tenant, monkeypatch, milan,
                          ExtractedFact("Ana", "lives_in", "Milan", 0.9, subject_type="person"))
        rome = db.get(AgentFact, rome.id)
        rome.invalid_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        rome.invalidated_by_id = true_one.id
        db.commit()

        fakes = [{"id": str(x.id), "content": x.content} for x in (happened, refuted, milan)]
        monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, *a, **k: fakes)
        ctx = agent_context.assemble_context(db, tenant.id, query="what did Ana do last week",
                                             end_user_id="ana", token_budget=800)["context"]
        assert "I redeemed a $5 coupon on coffee creamer last Sunday." in ctx
        assert "Ana lives in Milan." in ctx
        assert "Ana lives in Rome." not in ctx
