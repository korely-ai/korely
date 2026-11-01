"""Every judgement of a write weighs the facts as they were before it (memory_core 3.1.3, 2026-10-06).

The Cloud acceptance test of 3.0.0, 11/12 twice, "Slack stays true" wrong: from
"Globex moved to the Starter plan." the extractor also wrote "Globex replaces
Business plan" in the past tense. Judged first, it closed the plan; the batch
of "subscribes_to Starter plan" then saw Slack as the only subscription and
closed it (rebuilt with the candidates: 7 of 8 with the judge of 2.11.0, 5 of
8 with that of 2.12.0; with the plan among the candidates 0 of 10 for both).

The rule (`store.one_write`): a fact an earlier statement of the same
write closed is still a candidate of the later judgements, as Zep/Graphiti
resolve all the edges of an episode against the graph before the episode
(`resolve_extracted_edges`, getzep/graphiti 689de29). Naming it again closes
nothing: what is true now does not widen.

The judge here is the one the acceptance test measured: with the old plan among the
candidates it names the plan, without it the only other value. Red on the
code before, on both paths (one fact at a time, and the batch).
"""
# Port notes: through korely-agent's write (`facts.service._persist_extracted`)
# on its tables: the batched judge switched by `service._BATCHED_JUDGE` (the
# hosted `agent_f3_batch`), the judge replaced at this product's seams
# (`facts.contradiction._classify` and `_classify_batch`, the same names as
# the hosted adapter's). No vectors and no vocabulary model (the suite has no
# provider key).
from __future__ import annotations

from unittest.mock import patch

import pytest

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts import contradiction
from korely_agent.services.facts import service as agents_service
from korely_agent.services.memory_core import store
from korely_agent.services.memory_core.fact_extractor import ExtractedFact



@pytest.fixture
def without_model(monkeypatch):
    monkeypatch.setattr(agents_service, "_embed_fact", lambda *a, **k: None)


class _AcceptanceJudge:
    """Names the old plan when it sees one, else the only other value, and
    only for the new Starter plan; remembers the candidates it saw."""

    def __init__(self, *, break_the_batch: bool = False):
        self.seen: list[list[str]] = []
        self.break_the_batch = break_the_batch

    def _choose(self, candidates, new: str) -> list:
        self.seen.append(sorted(c.object_label for c in candidates))
        if "Starter" not in new:
            return []
        plans = [c for c in candidates if c.object_label.endswith("plan")]
        return [c.id for c in (plans or candidates)]

    def __call__(self, candidates, new_subject, new_predicate_raw, new_object, *a, **k):
        return self._choose(candidates, new_object), {}

    def batch(self, groups, *a, **k):
        if self.break_the_batch:
            raise RuntimeError("judge down")
        return {g["new_id"]: self._choose(g["candidates"], g["new_text"]) for g in groups}


def _memory(db, tenant, text) -> AgentMemory:
    m = AgentMemory(tenant_id=tenant.id, end_user_id="acme", content=text)
    db.add(m)
    db.commit()
    return m


def _f(obj, *, tense="current"):
    return ExtractedFact(subject="Acme", predicate_raw="on_plan", object=obj,
                         confidence=0.9, subject_type="organization", tense=tense)


def _write(db, tenant, memory, facts, judge, *, batch):
    with patch.object(agents_service, "_BATCHED_JUDGE", batch), \
         patch.object(contradiction, "_classify", judge), \
         patch.object(contradiction, "_classify_batch", judge.batch):
        return agents_service._persist_extracted(db, tenant.id, memory=memory, extracted=facts)


def _open(db) -> set[str]:
    return {f.object_label for f in db.query(AgentFact).filter(AgentFact.invalid_at.is_(None))}


def _closed(db) -> set[str]:
    return {f.object_label for f in db.query(AgentFact).filter(AgentFact.invalid_at.isnot(None))}


@pytest.mark.parametrize("batch", [False, True], ids=["per_fact", "batched"])
class TestAsBeforeTheWrite:
    def _before(self, db, tenant, judge, batch):
        _write(db, tenant, _memory(db, tenant, "Acme is on the Business plan."), [_f("Business plan")],
                judge, batch=batch)
        _write(db, tenant, _memory(db, tenant, "Acme pays for Slack."), [_f("Slack")], judge, batch=batch)
        assert _open(db) == {"Business plan", "Slack"}

    def test_slack_stays_true_next_to_the_new_plan(self, db, tenant, without_model, batch):
        judge = _AcceptanceJudge()
        self._before(db, tenant, judge, batch)
        m = _memory(db, tenant, "Acme moved to the Starter plan.")
        _write(db, tenant, m, [_f("Business plan", tense="past"), _f("Starter plan")],
                judge, batch=batch)
        assert _open(db) == {"Slack", "Starter plan"}
        assert _closed(db) == {"Business plan"}
        # The judgement of the Starter plan saw the plan the past statement of
        # the same write had just closed.
        assert ["Business plan", "Slack"] in judge.seen

    def test_the_fact_closed_by_the_write_is_not_closed_twice(self, db, tenant, without_model, batch):
        judge = _AcceptanceJudge()
        self._before(db, tenant, judge, batch)
        m = _memory(db, tenant, "Acme moved to the Starter plan.")
        _write(db, tenant, m, [_f("Business plan", tense="past"), _f("Starter plan")],
                judge, batch=batch)
        plan = db.query(AgentFact).filter(AgentFact.object_label == "Business plan").one()
        # Closed by the past statement, when it was said; the judge naming it
        # again changed nothing.
        assert plan.tense == "past" and plan.invalidated_by_id is None
        assert store._CLOSED_BY_THE_WRITE not in db.info


def test_a_failed_write_leaves_nothing_on_the_session(db, tenant, without_model):
    judge = _AcceptanceJudge(break_the_batch=True)
    _write(db, tenant, _memory(db, tenant, "Acme is on the Business plan."),
            [_f("Business plan")], _AcceptanceJudge(), batch=True)
    m = _memory(db, tenant, "Acme moved to the Starter plan.")
    assert _write(db, tenant, m, [_f("Business plan", tense="past"), _f("Starter plan")],
                   judge, batch=True) == []
    assert store._CLOSED_BY_THE_WRITE not in db.info
    # The whole write rolled back: the plan is still true.
    assert _open(db) == {"Business plan"}


def test_outside_a_write_open_stays_what_it_was(db):
    is_open = AgentFact.invalid_at.is_(None)
    assert store.as_before_the_write(db, AgentFact, is_open) is is_open
    with store.one_write(db):
        assert store.as_before_the_write(db, AgentFact, is_open) is is_open
        store.mark_closed(db, "f1")
        assert store.as_before_the_write(db, AgentFact, is_open) is not is_open
    store.mark_closed(db, "f2")  # outside a write: nothing kept
    assert store._CLOSED_BY_THE_WRITE not in db.info
