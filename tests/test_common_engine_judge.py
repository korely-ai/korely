"""The contradiction judge of this engine: memory_core's (`judge.judge`,
`judge.close`), called with this install's provider (piece 3 of the common
facts engine, 0.1.15).

Until 2026-10-06 this file froze the judge as it was on 0.1.15-dev and proved
that, for every answer a model can give, the new code sent the same prompt
with the same settings and read the same verdict. The equivalence held in
every release since; the frozen copy kept the product's internal names alive
and proved nothing about a deliberate change (the code audit of 6 October).
The twelve answers are the same, each with the verdict it gives, said here.

Two behaviours that were deliberate differences from 0.1.15 stay tested:
  * `close` writes the closings at once (flush, BUG-189 in the hosted
    product), so a second closing of the same fact in one transaction finds
    it closed instead of closing it again under another fact;
  * an `existing_id` that is not a string is skipped like any other id the
    judge was not shown (`.strip` on a number was a 500 on a direct write).
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.core.llm import LLMResult
from korely_agent.db.models import AgentFact, PredicateCluster
from korely_agent.services.facts import contradiction
from korely_agent.services.facts.contradiction import JudgeUnavailable, JudgeUnreadable
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import judge


NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


class Model:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def complete(self, messages, **kw):
        self.calls.append((messages[0]["content"], {k: kw.get(k) for k in
                                                    ("model", "max_tokens", "temperature", "json_only")}))
        if isinstance(self.answer, Exception):
            raise self.answer
        return LLMResult(text=self.answer)


def fact(db, tenant, obj, pred="subscribes_to", closed=False):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="c4821",
                  subject_label="c4821", subject_canonical="c4821", predicate_raw=pred,
                  predicate_canonical=pred, object_label=obj, object_canonical=obj,
                  valid_from=NOW - timedelta(days=200), confidence=0.9,
                  invalid_at=NOW - timedelta(days=1) if closed else None)
    db.add(f)
    db.commit()
    return f


@pytest.fixture
def candidates(db, tenant):
    # One relation single-valued with its example, so the tag is in the lines.
    db.add(PredicateCluster(tenant_id=tenant.id, canonical="subscribes_to", is_functional=True,
                            family="ownership", member_count=1, example_object="Priority plan"))
    db.commit()
    return [fact(db, tenant, "Priority plan"), fact(db, tenant, "Slack", pred="uses")]


def run_the_judge(db, tenant, candidates, monkeypatch, answer):
    model = Model(answer)
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: model)
    try:
        out = contradiction._classify(
            candidates, "c4821", "moved_to", "Basic plan", db, tenant.id, "gemini-x",
            new_predicate_canonical="subscribes_to", new_functional=True,
            new_example="Priority plan")
    except (JudgeUnavailable, JudgeUnreadable) as e:
        return type(e).__name__, model
    ids, reasons = out
    name = {str(candidates[0].id): "a", str(candidates[1].id): "b"}
    return ([name[str(i)] for i in ids], {name[str(k)]: v for k, v in reasons.items()}), model


def test_the_call_the_judge_receives(db, tenant, candidates, monkeypatch):
    _out, model = run_the_judge(db, tenant, candidates, monkeypatch, '{"contradictions": []}')
    ((prompt, settings),) = model.calls
    assert settings == {"model": "gemini-x", "max_tokens": judge.MAX_TOKENS,
                        "temperature": 0.0, "json_only": True}
    assert "Basic plan" in prompt and "Priority plan" in prompt and "Slack" in prompt
    assert "single-valued" in prompt, "the tag the relation's vocabulary gives, for the judge to read"


def _a_b(candidates):
    return str(candidates[0].id), str(candidates[1].id)


@pytest.mark.parametrize("make, expected", [
    # Nothing contradicts.
    (lambda a, b: '{"contradictions": []}', ([], {})),
    # One contradiction, with its reason.
    (lambda a, b: json.dumps({"contradictions": [{"existing_id": a, "reason": "one plan at a time"}]}),
     (["a"], {"a": "one plan at a time"})),
    # An id the judge was not shown is skipped; a reason is kept to 200 characters.
    (lambda a, b: json.dumps({"contradictions": [{"existing_id": str(uuid.uuid4()), "reason": "x"},
                                                 {"existing_id": b, "reason": "y" * 300}]}),
     (["b"], {"b": "y" * 200})),
    # The same id twice counts once, with the first reason (none here).
    (lambda a, b: json.dumps({"contradictions": [{"existing_id": a}, {"existing_id": a, "reason": "again"}]}),
     (["a"], {"a": ""})),
    # Entries that are not objects, without an id, or with an empty one: skipped.
    (lambda a, b: json.dumps({"contradictions": ["not an object", {"reason": "no id"}, {"existing_id": ""}]}),
     ([], {})),
    # An id that is not a UUID: skipped.
    (lambda a, b: json.dumps({"contradictions": [{"existing_id": "not-a-uuid"}]}), ([], {})),
    # Words around the JSON: the JSON is read.
    (lambda a, b: f'Here is the JSON: {{"contradictions": [{{"existing_id": "{a}", "reason": "r"}}]}}',
     (["a"], {"a": "r"})),
    # A bare list, another key, nothing, or prose: no verdict, never "nothing contradicts".
    (lambda a, b: json.dumps([{"existing_id": a}]), "JudgeUnreadable"),
    (lambda a, b: json.dumps({"verdicts": []}), "JudgeUnreadable"),
    (lambda a, b: "", "JudgeUnreadable"),
    (lambda a, b: "the model rambled and never answered", "JudgeUnreadable"),
    # A call that fails: no answer, retried by the worker.
    (lambda a, b: RuntimeError("429 RESOURCE_EXHAUSTED"), "JudgeUnavailable"),
])
def test_every_answer_gives_its_verdict(db, tenant, candidates, monkeypatch, make, expected):
    out, _model = run_the_judge(db, tenant, candidates, monkeypatch, make(*_a_b(candidates)))
    assert out == expected


def test_an_id_that_is_not_a_string_is_skipped(db, tenant, candidates, monkeypatch):
    out, _model = run_the_judge(db, tenant, candidates, monkeypatch,
                        json.dumps({"contradictions": [{"existing_id": 42}]}))
    assert out == ([], {})


def test_no_provider_and_no_candidates_ask_nothing(db, tenant, candidates, monkeypatch):
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
    assert contradiction._classify(candidates, "c4821", "p", "o", db, tenant.id, "m") == ([], {})
    m = Model('{"contradictions": []}')
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: m)
    assert contradiction._classify([], "c4821", "p", "o", db, tenant.id, "m") == ([], {})
    assert m.calls == []


class TestTheClosing:
    def test_the_open_facts_close_at_the_date_under_the_fact(self, db, tenant):
        a, b, closed = fact(db, tenant, "A"), fact(db, tenant, "B"), fact(db, tenant, "C", closed=True)
        was_closed_at = closed.invalid_at
        by, when = fact(db, tenant, "Basic plan", pred="moved_to").id, NOW - timedelta(days=10)
        out = judge.close(db, AgentFact, [a.id, b.id, closed.id, uuid.uuid4()], by, when,
                             is_open=true_at())
        assert out == [a.id, b.id], "the open ones, in order; the closed one and an unknown id are not"
        db.expire_all()
        for f in (a, b):
            got = db.get(AgentFact, f.id)
            assert (got.invalid_at, got.invalidated_by_id) == (when, by)
        assert db.get(AgentFact, closed.id).invalid_at == was_closed_at

    def test_a_second_closing_finds_it_closed(self, db, tenant):
        """BUG-189: two new facts of one write that contradict the same old one."""
        f = fact(db, tenant, "Priority plan")
        first = fact(db, tenant, "Basic plan", pred="moved_to").id
        second = fact(db, tenant, "Free plan", pred="moved_to").id
        # A date in the past: a fact closed on a future date is still true
        # today (true_at), and would rightly be closable again.
        when = datetime.now(timezone.utc) - timedelta(minutes=1)
        assert judge.close(db, AgentFact, [f.id], first, when, is_open=true_at()) == [f.id]
        assert judge.close(db, AgentFact, [f.id], second, when, is_open=true_at()) == []
        assert db.get(AgentFact, f.id).invalidated_by_id == first

    def test_an_end_already_given_is_never_moved_later(self, db, tenant):
        """A fact true now with an end ahead (a forget's `at`, an earlier
        closing): a new fact that begins at or after that end leaves it, and
        its closer, where they are; one that begins before it closes the
        fact there, as before (a review finding of 9 Nov 2026; Graphiti
        `resolve_edge_contradictions`)."""
        now = datetime.now(timezone.utc)
        ends = now + timedelta(days=2)
        f = fact(db, tenant, "Priority plan")
        f.invalid_at = ends
        db.commit()
        later = fact(db, tenant, "Basic plan", pred="moved_to").id
        same = fact(db, tenant, "Free plan", pred="moved_to").id
        sooner = fact(db, tenant, "Team plan", pred="moved_to").id
        assert judge.close(db, AgentFact, [f.id], later, now + timedelta(days=10),
                           is_open=true_at()) == []
        assert judge.close(db, AgentFact, [f.id], same, ends, is_open=true_at()) == []
        db.expire_all()
        got = db.get(AgentFact, f.id)
        assert (got.invalid_at, got.invalidated_by_id) == (ends, None)
        assert judge.close(db, AgentFact, [f.id], sooner, now + timedelta(days=1),
                           is_open=true_at()) == [f.id]
        db.expire_all()
        got = db.get(AgentFact, f.id)
        assert (got.invalid_at, got.invalidated_by_id) == (now + timedelta(days=1), sooner)
