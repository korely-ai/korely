"""memory_core 2.12.0: a phrase in the past ends when it is said, a plan that
comes true goes through the judge; 3.0.0: the plan stays in the history.

The code audit of 6 October found:
  * "I used to work at Acme", said in 2023, was true from then until the day
    of the write, and came back with as_of=2024;
  * a plan said again as a fact became `current` without the judge, and
    "uses Jira" stayed true beside "uses Linear".
The decision of 6 October (3.0.0): the plan is not promoted in place.
Its row closes when it comes true and the fact that came true is a new row,
judged like every new fact; `as_of` before it came true returns the plan
(test_engine_plan_come_true.py for the rest).

No model call: extraction and judge are fake.
"""
# Port notes: `write_fact` on korely-agent's tables (`facts.entities.TABLES`,
# tenant_id). The two "from the cloud" tests run through korely-agent's write
# (`facts.service.extract_and_persist_facts`): extraction faked with
# tests.conftest.stub_extraction, the fake judge behind this product's provider
# (`core.llm.get_provider`), the batched judge switched by
# `service._BATCHED_JUDGE`, the vocabulary without a model
# (`predicate_vocabulary_model=False`), the reads by
# `facts.retrieve_agent_facts` and the public ids by `services.ids`.
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from korely_agent.core.llm import LLMResult
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import predicates
from korely_agent.services.facts import entities
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core.store import Relation
from korely_agent.services.memory_core.writing import write_fact
from tests.conftest import stub_extraction
from korely_agent.services.sentinels import EVERY_PROJECT



def _d(*a):
    return datetime(*a, tzinfo=timezone.utc)


def _aware(v):
    return None if v is None else (v if v.tzinfo else v.replace(tzinfo=timezone.utc))


WORKS_AT = Relation(canonical="works_at", is_functional=True, family="employment", example="Acme")


class _Writer:
    """`write_fact` on the cloud's tables (SQLite), with the fake judge that
    records what reaches it and closes what it is told to."""

    def __init__(self, db, tenant):
        self.db, self.tenant = db, tenant
        self.judged = []
        self.close = []

    def write(self, obj, *, tense="current", valid_from=None, text_valid_from=None, arrival=None,
               relation=WORKS_AT, predicate="works at", subject="Maria", defer=False):
        m = AgentMemory(tenant_id=self.tenant.id, end_user_id="u1", content="x", graph_sync_status="done")
        self.db.add(m)
        self.db.commit()

        def judge(g):
            self.judged.append(g)
            return list(self.close)

        return write_fact(
            self.db, entities.TABLES, entities.memory_of(self.tenant.id, None, "u1", None),
            columns=dict(tenant_id=self.tenant.id, project_id=None, end_user_id="u1", agent_id=None, run_id=None),
            relation=relation, is_open=true_at(), embed=lambda f: None, judge=judge,
            subject=subject, subject_type="person", predicate_raw=predicate, obj=obj,
            object_is_literal=False, confidence=0.9, source_memory_id=m.id, model_used=None,
            valid_from=valid_from, text_valid_from=text_valid_from, arrival=arrival, tense=tense,
            defer=defer,
        )

    def true_on(self, when):
        rows = self.db.query(AgentFact).filter(AgentFact.tenant_id == self.tenant.id, true_at(when)).all()
        return {r.object_label for r in rows}


class TestThePhraseInThePast:
    def test_said_in_2023_is_not_true_in_2024(self, db, tenant):
        w = _Writer(db, tenant)
        fact, _ = w.write("Acme", tense="past", valid_from=_d(2023, 3, 1))
        assert fact.tense == "past" and _aware(fact.invalid_at) == _d(2023, 3, 1)
        assert w.true_on(_d(2024, 1, 1)) == set()
        assert w.true_on(datetime.now(timezone.utc)) == set()

    def test_with_the_start_date_in_the_text_true_until_it_is_said(self, db, tenant):
        """The text says when it began (RULES_DATE), the arrival when it was
        said: true in 2020, not in 2024."""
        w = _Writer(db, tenant)
        fact, _ = w.write("Acme", tense="past", text_valid_from=_d(2019, 1, 1), arrival=_d(2023, 3, 1))
        assert _aware(fact.valid_from) == _d(2019, 1, 1) and _aware(fact.invalid_at) == _d(2023, 3, 1)
        assert w.true_on(_d(2020, 6, 1)) == {"Acme"}
        assert w.true_on(_d(2024, 1, 1)) == set()

    def test_never_closed_before_beginning(self, db, tenant):
        w = _Writer(db, tenant)
        future = _d(2099, 1, 1)
        fact, _ = w.write("Acme", tense="past", valid_from=future)
        assert _aware(fact.invalid_at) >= _aware(fact.valid_from)

    def test_the_twin_closes_when_said_not_when_it_began(self, db, tenant):
        """Before: closed on 2019-01-01 (the text's date, the start), before
        the time the memory still said it was true."""
        w = _Writer(db, tenant)
        true_one, _ = w.write("Acme", valid_from=_d(2018, 1, 1))
        same, _ = w.write("Acme", tense="past", text_valid_from=_d(2019, 1, 1), arrival=_d(2023, 3, 1))
        assert same.id == true_one.id and same.tense == "past"
        assert _aware(same.invalid_at) == _d(2023, 3, 1)
        assert w.true_on(_d(2022, 1, 1)) == {"Acme"} and w.true_on(_d(2024, 1, 1)) == set()

    def test_the_twin_that_began_later_stays_true_and_the_phrase_is_history(self, db, tenant):
        """Said in 2024 ("I worked at Acme"), written after a 2025 memory that
        says they work there: it is back. Before: the 2025 twin closed now."""
        w = _Writer(db, tenant)
        true_one, _ = w.write("Acme", valid_from=_d(2025, 6, 1))
        history, _ = w.write("Acme", tense="past", valid_from=_d(2024, 1, 1))
        assert history.id != true_one.id and history.tense == "past"
        assert _aware(history.invalid_at) == _d(2024, 1, 1)
        assert true_one.invalid_at is None and true_one.tense == "current"
        assert w.true_on(datetime.now(timezone.utc)) == {"Acme"}

    def test_facts_on_the_same_entities_close_when_said(self, db, tenant):
        w = _Writer(db, tenant)
        w.write("Acme", tense="past", text_valid_from=_d(2019, 1, 1), arrival=_d(2023, 3, 1))
        [g] = w.judged
        assert g.past is True and _aware(g.when) == _d(2023, 3, 1)


class TestThePlanThatComesTrue:
    USES = Relation(canonical="uses", is_functional=True, family="tools", example="Jira")

    def test_goes_to_the_judge_and_begins_when_it_comes_true(self, db, tenant):
        """3.0.0: a new row, judged; the plan closes where it begins, with its
        date from before."""
        w = _Writer(db, tenant)
        jira, _ = w.write("Jira", relation=self.USES, predicate="uses", subject="Globex",
                           valid_from=_d(2024, 6, 1))
        plan, _ = w.write("Linear", tense="planned", relation=self.USES, predicate="uses",
                            subject="Globex", valid_from=_d(2025, 1, 1))
        w.judged.clear()
        w.close = [jira.id]
        linear, closed = w.write("Linear", relation=self.USES, predicate="uses", subject="Globex",
                                  valid_from=_d(2025, 6, 1))
        assert linear.id != plan.id and linear.tense == "current" and linear.invalid_at is None
        assert _aware(linear.valid_from) == _d(2025, 6, 1)
        assert plan.tense == "planned" and _aware(plan.valid_from) == _d(2025, 1, 1)
        assert _aware(plan.invalid_at) == _d(2025, 6, 1) and plan.invalidated_by_id == linear.id
        assert closed == [jira.id, plan.id]
        [g] = w.judged
        assert g.fact_id == linear.id and _aware(g.when) == _d(2025, 6, 1) and g.past is False

    def test_also_in_a_batched_write(self, db, tenant):
        """The fact that came true was born of the memory being written: the
        batch weighs it with the others, no call of its own. The closed plan is
        in the answer."""
        w = _Writer(db, tenant)
        plan, _ = w.write("Linear", tense="planned", relation=self.USES, predicate="uses", subject="Globex")
        w.judged.clear()
        linear, closed = w.write("Linear", relation=self.USES, predicate="uses", subject="Globex",
                                  defer=True)
        assert w.judged == [] and closed == [plan.id]
        assert linear.id != plan.id and linear.invalid_at is None and plan.invalid_at is not None

    def test_said_again_as_a_plan_is_not_judged(self, db, tenant):
        w = _Writer(db, tenant)
        w.write("Linear", tense="planned", relation=self.USES, predicate="uses", subject="Globex")
        w.judged.clear()
        same, closed = w.write("Linear", tense="planned", relation=self.USES, predicate="uses",
                                  subject="Globex")
        assert same.tense == "planned" and w.judged == [] and closed == []


# ------------------------------------------- from the cloud, with as_of and the webhooks

def _judge_that_sees_contradictions(prompt, max_tokens):
    if "[new_id=" in prompt:
        results = []
        for block in prompt.split("[new_id=")[1:]:
            nid = block.split("]", 1)[0]
            ids = re.findall(r"- id=([0-9a-f-]{36})", block)
            results.append({"new_id": nid, "contradictions": [{"existing_id": i} for i in ids]})
        return json.dumps({"results": results})
    ids = re.findall(r"- id=([0-9a-f-]{36})", prompt)
    return json.dumps({"contradictions": [{"existing_id": i} for i in ids]})


class _Provider:
    """The fake judge behind this product's provider (`core.llm`)."""

    def complete(self, messages, **kw):
        return LLMResult(text=_judge_that_sees_contradictions(messages[0]["content"], kw.get("max_tokens")))


@pytest.fixture
def cloud(monkeypatch):
    from korely_agent.services.facts import service as svc
    from korely_agent.services.memory_core.fact_extractor import ExtractedFact

    monkeypatch.setattr(svc, "_embed_fact", lambda *a, **k: None)
    provider = _Provider()
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: provider)
    monkeypatch.setattr(predicates, "_ask", lambda: None)

    def extract(text, **k):
        tense, obj = text.split("|")
        return [ExtractedFact(subject="Globex", predicate_raw="uses", object=obj, confidence=0.9,
                              subject_type="organization", tense=tense)]

    stub_extraction(monkeypatch, svc, extract)
    return svc


def _write(svc, db, tenant, text, event):
    m = AgentMemory(tenant_id=tenant.id, end_user_id="u1", content=text, event_time=event,
                    graph_sync_status="pending")
    db.add(m)
    db.commit()
    db.refresh(m)
    return svc.extract_and_persist_facts(db, tenant.id, memory=m, valid_from=event)


@pytest.mark.parametrize("batched", [False, True], ids=["single_judge", "batched_judge"])
def test_jira_then_linears_plan_then_linear(cloud, db, tenant, monkeypatch, batched):
    from korely_agent.services import facts as agent_facts
    from korely_agent.services.ids import fact_public_id

    monkeypatch.setattr(cloud, "_BATCHED_JUDGE", batched)
    _write(cloud, db, tenant, "current|Jira", _d(2024, 6, 1))
    _write(cloud, db, tenant, "planned|Linear", _d(2025, 1, 1))
    shapes = _write(cloud, db, tenant, "current|Linear", _d(2025, 6, 1))

    rows = {(r.object_label, r.tense): r
             for r in db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id)}
    linear, plan, jira = rows[("Linear", "current")], rows[("Linear", "planned")], rows[("Jira", "current")]
    assert linear.invalid_at is None and _aware(linear.valid_from) == _d(2025, 6, 1)
    assert _aware(jira.invalid_at) == _d(2025, 6, 1) and jira.invalidated_by_id == linear.id
    # 3.0.0: the plan stays in the history, with its date, closed by the fact that came true.
    assert _aware(plan.valid_from) == _d(2025, 1, 1) and _aware(plan.invalid_at) == _d(2025, 6, 1)
    assert plan.invalidated_by_id == linear.id
    # The write's shape names the closed facts (and the webhook with it).
    assert [f["invalidated"] for f in shapes] == [[fact_public_id(jira.id), fact_public_id(plan.id)]]

    def as_it_was(when):
        rows, _ = agent_facts.retrieve_agent_facts(db, tenant.id, as_of=when, project_id=EVERY_PROJECT)
        return {(r.object_label, r.tense) for r in rows}

    # Before it comes true: Jira and the plan; after: Linear only.
    assert as_it_was(_d(2024, 9, 1)) == {("Jira", "current")}
    assert as_it_was(_d(2025, 3, 1)) == {("Jira", "current"), ("Linear", "planned")}
    assert as_it_was(_d(2025, 9, 1)) == {("Linear", "current")}


def test_past_from_the_cloud_with_as_of(cloud, db, tenant):
    from korely_agent.services import facts as agent_facts

    _write(cloud, db, tenant, "past|Salesforce", _d(2023, 3, 1))
    rows, _ = agent_facts.retrieve_agent_facts(db, tenant.id, as_of=_d(2024, 1, 1), project_id=EVERY_PROJECT)
    assert rows == []
    history, _ = agent_facts.retrieve_agent_facts(db, tenant.id, include_invalidated=True, project_id=EVERY_PROJECT)
    assert [(r.object_label, r.tense) for r in history] == [("Salesforce", "past")]
