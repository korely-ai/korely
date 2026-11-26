"""memory_core 6.19.0: a change told as an event can end the value it
replaced, when two readings of the judge name that value.

The finding (Korely Cloud 1.0.33, 18 Nov 2026), told with a made-up example
of the same shape: "Tom is on the Gold plan", then "Tom downgraded from Gold
to Basic". With the Cloud's European writer the only fact was the event, in
the past ("Tom downgrades_to Basic"): born closed, weighed only against the
facts about Tom and Basic, so the judge was never asked about Gold and Gold
stayed current.

What must hold (`writing._the_value_it_replaced`):
  * off (the default), every write is the 6.17.0 one: the same judge calls;
  * on, another value closes only when the wider past reading AND the
    present reading name the same fact;
  * the facts about the statement's own two entities are judged as before,
    never closed by the wider reading (the two wrong closings of LoCoMo
    conversation 26 on the branch of 18 Nov 2026 were of that kind);
  * the statement that ended a value holds as the new state and the batch of
    the write does not judge it again;
  * the product passes the switch only for a fact read from a memory, in the
    past, by the writer the extraction rules call gpt-oss.

No model call: extraction and judge are fake.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM, LLMResult
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import predicates
from korely_agent.services.facts import entities
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import store as mc_store
from korely_agent.services.memory_core import worker as mc_worker
from korely_agent.services.memory_core.contradiction import PAST_MARK
from korely_agent.services.memory_core.store import Relation
from korely_agent.services.memory_core.writing import write_fact
from tests.conftest import stub_extraction


def _d(*a):
    return datetime(*a, tzinfo=timezone.utc)


def _aware(v):
    return None if v is None else (v if v.tzinfo else v.replace(tzinfo=timezone.utc))


PLAN = Relation(canonical="subscribed_to", is_functional=True, family="ownership", example="Gold")
EVENT = Relation(canonical="downgrades_to", is_functional=None, family="events", example="Basic")
BILLING = Relation(canonical="pays_with", is_functional=None, family="financial", example="annual billing")


class _Writer:
    """`write_fact` on this product's tables with a scripted judge: `answer(g)`
    gets the `ToJudge` and returns the ids the judge names."""

    def __init__(self, db, tenant):
        self.db, self.tenant = db, tenant
        self.asked = []
        self.answer = lambda g: []

    def write(self, obj, *, relation, predicate, tense="current", arrival=None, past_event=False):
        m = AgentMemory(tenant_id=self.tenant.id, end_user_id="u1", content="x", graph_sync_status="done")
        self.db.add(m)
        self.db.commit()

        def judge(g):
            self.asked.append(g)
            return list(self.answer(g))

        with mc_store.one_write(self.db):
            out = write_fact(
                self.db, entities.TABLES, entities.memory_of(self.tenant.id, None, "u1", None),
                columns=dict(tenant_id=self.tenant.id, project_id=None, end_user_id="u1", agent_id=None,
                             run_id=None),
                relation=relation, is_open=true_at(), embed=lambda f: None, judge=judge,
                subject="Tom", subject_type="person", predicate_raw=predicate, obj=obj,
                object_is_literal=False, confidence=0.9, source_memory_id=m.id, model_used=None,
                arrival=arrival, tense=tense, past_event=past_event,
            )
            judged = mc_store.judged_by_the_write(self.db)
        self.db.commit()
        return out, judged

    def open_objects(self):
        rows = self.db.query(AgentFact).filter(AgentFact.tenant_id == self.tenant.id, true_at()).all()
        return {r.object_label for r in rows}


def _readings(asked):
    return [(g.past, g.among_its_subject, g.ask_only) for g in asked]


@pytest.fixture
def w(db, tenant):
    return _Writer(db, tenant)


def _gold(w):
    (gold, _), _j = w.write("Gold", relation=PLAN, predicate="subscribed_to", arrival=_d(2026, 10, 15))
    w.asked.clear()
    return gold


def test_off_by_default_the_write_is_the_6_17_0_one(w):
    gold = _gold(w)
    w.answer = lambda g: [gold.id]  # whatever it is asked, it names Gold
    (event, closed), judged = w.write("Basic", relation=EVENT, predicate="downgrades_to", tense="past",
                                      arrival=_d(2026, 11, 18))
    # One judgement, on the facts about Tom and Basic, as before 6.19.0.
    assert _readings(w.asked) == [(True, False, False)]
    assert event.tense == "past" and _aware(event.invalid_at) == _d(2026, 11, 18)
    assert judged == frozenset()
    assert closed == [gold.id]  # the fake answer of the per-fact judge, returned as it was


def test_the_event_ends_the_value_both_readings_name(w, db):
    gold = _gold(w)

    def answer(g):
        if not g.among_its_subject:
            return []        # nothing about Tom and Basic
        return [gold.id]     # the wider past reading and the present one both name Gold

    w.answer = answer
    (event, closed), judged = w.write("Basic", relation=EVENT, predicate="downgrades_to", tense="past",
                                      arrival=_d(2026, 11, 18), past_event=True)
    assert _readings(w.asked) == [(True, False, False), (True, True, True), (False, True, True)]
    db.refresh(gold)
    assert closed == [gold.id]
    assert _aware(gold.invalid_at) == _d(2026, 11, 18) and gold.invalidated_by_id == event.id
    # The event holds as the new state, from when it was said, and the batch
    # of the write does not judge it again.
    assert event.tense == "current" and event.invalid_at is None
    assert judged == frozenset({event.id})
    assert w.open_objects() == {"Basic"}
    # The present reading is shown as a present fact, with its tag and date.
    present = w.asked[2]
    assert present.when == _d(2026, 11, 18) and present.functional is False


def test_one_reading_is_not_enough(w, db):
    gold = _gold(w)
    w.answer = lambda g: [gold.id] if (g.among_its_subject and g.past) else []
    (event, closed), judged = w.write("Basic", relation=EVENT, predicate="downgrades_to", tense="past",
                                      arrival=_d(2026, 11, 18), past_event=True)
    db.refresh(gold)
    assert closed == [] and gold.invalid_at is None
    # History, as before 6.19.0.
    assert event.tense == "past" and _aware(event.invalid_at) == _d(2026, 11, 18)
    assert event.invalidated_by_id is None and judged == frozenset()


def test_the_two_readings_must_name_the_same_fact(w, db):
    gold = _gold(w)
    (billing, _), _j = w.write("annual billing", relation=BILLING, predicate="pays_with", arrival=_d(2026, 10, 20))
    w.asked.clear()

    def answer(g):
        if not g.among_its_subject:
            return []
        return [gold.id] if g.past else [billing.id]

    w.answer = answer
    (event, closed), _judged = w.write("Basic", relation=EVENT, predicate="downgrades_to", tense="past",
                                       arrival=_d(2026, 11, 18), past_event=True)
    db.refresh(gold)
    db.refresh(billing)
    assert closed == [] and gold.invalid_at is None and billing.invalid_at is None
    assert event.tense == "past"


def test_its_own_two_entities_are_never_closed_by_the_wider_reading(w, db):
    """The two wrong closings of LoCoMo conversation 26 on the branch of 18 Nov:
    "feels accepted" by "felt accepted and happy", named among ten candidates
    and not when asked alone. Here what the per-fact judge of 6.17.0 left open
    stays open, whatever the wider readings say."""
    (feels, _), _j = w.write("accepted", relation=Relation(canonical="feels", is_functional=None,
                                                           family="other", example="happy"),
                             predicate="feels", arrival=_d(2026, 5, 1))
    w.asked.clear()
    w.answer = lambda g: [] if not g.among_its_subject else [feels.id]
    (said, closed), judged = w.write("accepted", relation=Relation(canonical="felt", is_functional=None,
                                                                   family="other", example="happy"),
                                     predicate="felt", tense="past", arrival=_d(2026, 5, 8), past_event=True)
    db.refresh(feels)
    assert feels.invalid_at is None and closed == []
    # Not even asked a second time: there was no other value to confirm.
    assert _readings(w.asked) == [(True, False, False), (True, True, True)]
    assert said.tense == "past" and judged == frozenset()


def test_what_the_first_judgement_closed_is_not_counted_again(w, db):
    gold = _gold(w)
    w.answer = lambda g: [gold.id]
    (event, closed), _judged = w.write("Basic", relation=EVENT, predicate="downgrades_to", tense="past",
                                       arrival=_d(2026, 11, 18), past_event=True)
    # The per-fact judge of 6.17.0 named Gold (the fake does not close it); the
    # wider readings name only what it had not named already: nothing more.
    assert closed == [gold.id]
    assert _readings(w.asked) == [(True, False, False), (True, True, True)]
    assert event.tense == "past"


def test_a_row_of_the_same_triple_dated_ahead_keeps_it_history(w, db):
    """The open-triple index allows one open row: a statement that would
    become open beside a row of its own triple dated ahead stays history."""
    gold = _gold(w)
    (ahead, _), _j = w.write("Basic", relation=EVENT, predicate="downgrades_to", arrival=_d(2099, 1, 1))
    w.asked.clear()
    w.answer = lambda g: [gold.id] if g.among_its_subject else []
    (event, closed), judged = w.write("Basic", relation=EVENT, predicate="downgrades_to", tense="past",
                                      arrival=_d(2026, 11, 18), past_event=True)
    db.refresh(gold)
    assert event.id != ahead.id and event.tense == "past" and event.invalid_at is not None
    assert closed == [] and gold.invalid_at is None and judged == frozenset()


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the blocker of 22 Nov 2026 (writing._the_value_it_replaced): the "
                   "switch stays off until a later memory telling the event again keeps the state it brought")
def test_an_event_told_again_keeps_the_state_it_brought(w, db):
    gold = _gold(w)
    w.answer = lambda g: [gold.id] if g.among_its_subject else []
    (event, _), _j = w.write("Basic", relation=EVENT, predicate="downgrades_to", tense="past",
                             arrival=_d(2026, 11, 18), past_event=True)
    assert event.tense == "current"
    # A later note mentions the downgrade again, in the past.
    (again, _), _j = w.write("Basic", relation=EVENT, predicate="downgrades_to", tense="past",
                             arrival=_d(2026, 11, 20), past_event=True)
    db.refresh(event)
    assert again.id == event.id and event.invalid_at is None
    assert w.open_objects() == {"Basic"}


def test_the_batch_leaves_out_what_the_write_already_judged():
    class F:
        def __init__(self, i):
            self.id, self.source_memory_id, self.tense, self.invalid_at = i, "m", "current", None
            self.observation_count = 1
            self.end_user_id = self.subject_label = self.subject_canonical = None
            self.predicate_raw = self.predicate_canonical = self.object_label = None
            self.object_canonical = self.embedding = self.valid_from = None

    a, b = F(1), F(2)
    picked = mc_worker._new_for_the_batch([(a, []), (b, [])], "m", (), frozenset({2}))
    assert [f["id"] for f in picked] == [1]
    assert [f["id"] for f in mc_worker._new_for_the_batch([(a, []), (b, [])], "m")] == [1, 2]


# --- the product: who gets the switch --------------------------------------

@pytest.mark.parametrize("on,writer,source,named,tense,expected", [
    (False, "gpt-oss", "m", False, "past", False),
    (True, "gemini", "m", False, "past", False),
    (True, None, "m", False, "past", False),
    (True, "gpt-oss", None, False, "past", False),
    (True, "gpt-oss", "m", True, "past", False),
    (True, "gpt-oss", "m", False, "current", False),
    (True, "gpt-oss", "m", False, "past", True),
], ids=["off", "gemini", "another-writer", "no-memory", "named-by-the-caller", "present", "gpt-oss"])
def test_the_switch_goes_only_to_the_measured_writer(monkeypatch, on, writer, source, named, tense, expected):
    from korely_agent.services.facts import extractor, service as svc

    monkeypatch.setattr(svc, "_PAST_EVENT", on)
    monkeypatch.setattr(extractor, "measured_writer", lambda model: writer)
    assert svc._past_event(source, named, "a-model", tense) is expected


VEC = [1.0] + [0.0] * (EMBEDDING_DIM - 1)


class _Judge:
    """The judge behind this product's provider: names the candidate lines
    that hold `target`, in the reading `when(prompt)` accepts."""

    def __init__(self, target, when):
        self.target, self.when, self.prompts = target, when, []

    def complete(self, messages, **kw):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        if "[new_id=" in prompt:
            parts = re.split(r"\[new_id=([^\]]+)\] NEW: ", prompt)
            return LLMResult(text=json.dumps({"results": [
                {"new_id": parts[i], "contradictions": []} for i in range(1, len(parts) - 1, 2)]}))
        existing = prompt.split("EXISTING facts:", 1)[1].split("NEW fact", 1)[0]
        ids = [m.group(1) for m in re.finditer(r"id=([0-9a-f-]{36})[^\n]*", existing)
               if self.target in m.group(0)] if self.when(prompt) else []
        return LLMResult(text=json.dumps({"contradictions": [{"existing_id": i} for i in ids]}))


@pytest.fixture
def product(monkeypatch):
    from korely_agent.services.facts import extractor, service as svc
    from korely_agent.services.memory_core.fact_extractor import ExtractedFact

    def vector(db, tenant_id, fact):
        fact.embedding = VEC
        fact.embedding_status = "done"

    monkeypatch.setattr(svc, "_embed_fact", vector)
    monkeypatch.setattr(predicates, "_ask", lambda: None)
    monkeypatch.setattr(extractor, "measured_writer", lambda model: "gpt-oss")

    def extract(text, **k):
        tense, relation, obj = text.split("|")
        return [ExtractedFact(subject="Tom", predicate_raw=relation, object=obj, confidence=0.9,
                              subject_type="person", tense=tense)]

    stub_extraction(monkeypatch, svc, extract)
    return svc


def _remember(svc, db, tenant, text, when):
    m = AgentMemory(tenant_id=tenant.id, end_user_id="u1", content=text, event_time=when,
                    graph_sync_status="pending")
    db.add(m)
    db.commit()
    db.refresh(m)
    return svc.extract_and_persist_facts(db, tenant.id, memory=m, valid_from=when)


def _plan_rows(db, tenant):
    return {r.object_label: r for r in db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id)}


@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
def test_downgrade_told_as_an_event_through_the_write(product, db, tenant, monkeypatch, on):
    judge = _Judge("Gold", when=lambda prompt: True)
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: judge)
    # raising=False: on the code before 6.19.0 there is no switch, and the
    # test fails on what the store holds, not on a missing name.
    monkeypatch.setattr(product, "_PAST_EVENT", on, raising=False)
    _remember(product, db, tenant, "current|subscribed_to|Gold", _d(2026, 10, 15))
    judge.prompts.clear()
    _remember(product, db, tenant, "past|downgrades_to|Basic", _d(2026, 11, 18))
    rows = _plan_rows(db, tenant)
    gold, basic = rows["Gold"], rows["Basic"]
    if not on:
        # 6.17.0: the event is history, Gold stays current, the judge is not asked.
        assert gold.invalid_at is None and basic.tense == "past" and judge.prompts == []
        return
    assert _aware(gold.invalid_at) == _d(2026, 11, 18) and gold.invalidated_by_id == basic.id
    assert basic.tense == "current" and basic.invalid_at is None
    # Two readings, the first with the past mark; no batch judged the event again.
    assert len(judge.prompts) == 2
    assert PAST_MARK in judge.prompts[0] and PAST_MARK not in judge.prompts[1]
    assert not any("[new_id=" in p for p in judge.prompts)


def test_through_the_write_a_reading_that_does_not_agree_closes_nothing(product, db, tenant, monkeypatch):
    judge = _Judge("Gold", when=lambda prompt: PAST_MARK in prompt)  # only the past reading names Gold
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: judge)
    monkeypatch.setattr(product, "_PAST_EVENT", True)
    _remember(product, db, tenant, "current|subscribed_to|Gold", _d(2026, 10, 15))
    _remember(product, db, tenant, "past|downgrades_to|Basic", _d(2026, 11, 18))
    rows = _plan_rows(db, tenant)
    assert rows["Gold"].invalid_at is None
    assert rows["Basic"].tense == "past" and _aware(rows["Basic"].invalid_at) == _d(2026, 11, 18)
