"""With no end user, an agent's write closes only that agent's facts (memory_core 6.28.0).

A product decision of 26 Nov 2026 on a team's memory: when two agents write
facts on the same thing, the judge compares a new fact only with the facts
of the same `agent_id`, as Mem0 does; an agent
cannot close another agent's facts. A read with an `agent_id` already showed
that agent's facts only, so a closing by another agent's fact was one its
reader never saw: the old value gone from its block, the new one not in it.

The two choices of the piece (`owner.judged_within_its_agent`):
  * a write with no agent (the project's memory with no end user) meets
    only the facts written with no agent, and an agent's write never meets
    them: the default namespace is a namespace of its own;
  * with an end user nothing changes: the end user's facts are one person's
    memory, weighed together whichever agent wrote them.

Every door of the judge is here: the batched judgement of an extraction's
write (the default), the one fact at a time, a fact posted directly, and the
late judgement of a vector. The judge is a fake that closes every candidate
it is shown; the vectors put every fact at distance 0. No model, no network
(tests/conftest.py). The tests marked "new" fail on memory_core 6.27.0
(v1.0.40, c86e149e); the others hold on both and guard the same agent's own
closings, which a door that forgot to pass the agent would lose.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import vectors
from korely_agent.services.facts import contradiction as contra
from korely_agent.services.facts import service
from korely_agent.services.memory_core import owner, worker
from korely_agent.services.memory_core.fact_extractor import ExtractedFact

VEC = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
SUBJECT, RELATION = "the product repository", "has_status"


def day(d: int) -> datetime:
    return datetime(2026, 11, d, tzinfo=timezone.utc)


@pytest.fixture
def shown(monkeypatch):
    """The judge's fake: closes every candidate; records, per judgement, the
    new fact's object and its candidates' objects."""
    seen = []

    def one(cands, new_subject, new_predicate_raw, new_object, *a, **k):
        seen.append((new_object, sorted(c.object_label for c in cands)))
        return [c.id for c in cands], {}

    def batch(groups, db, tenant_id, model_name=None):
        out = {}
        for g in groups:
            new = db.get(AgentFact, uuid.UUID(g["new_id"]))
            seen.append((new.object_label, sorted(c.object_label for c in g["candidates"])))
            out[g["new_id"]] = [c.id for c in g["candidates"]]
        return out

    monkeypatch.setattr(service, "fact_vector", lambda text: list(VEC))
    monkeypatch.setattr(contra, "_classify", one)
    monkeypatch.setattr(contra, "_classify_batch", batch)
    monkeypatch.setattr("korely_agent.services.webhooks.emit_event", lambda *a, **k: None)
    return seen


@pytest.fixture(params=[True, False], ids=["batched", "one-by-one"])
def judged(request, monkeypatch):
    monkeypatch.setattr(service, "_BATCHED_JUDGE", request.param)
    return request.param


def remember(db, tenant, obj, *, agent=None, user=None, when=None):
    """One memory with one fact, written as the extraction writes it."""
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=user, agent_id=agent,
                    content=f"{SUBJECT} {RELATION} {obj}")
    db.add(m)
    db.commit()
    service._persist_extracted(db, tenant.id, memory=m, valid_from=when, extracted=[
        ExtractedFact(subject=SUBJECT, predicate_raw=RELATION, object=obj, confidence=0.9)])
    return m


def open_values(db, tenant) -> dict:
    """{object: agent} of the facts still true."""
    return {f.object_label: f.agent_id for f in db.query(AgentFact).filter(
        AgentFact.tenant_id == tenant.id, AgentFact.invalid_at.is_(None))}


# 1. The write of a memory: batched and one fact at a time ----------------------

def test_new_another_agents_fact_is_never_shown_nor_closed(db, tenant, shown, judged):
    """New. The case of the team's memory (7 Nov 2026, row 17 of that replay):
    one agent wrote the repository private, another suspended; on 6.27.0 the
    second write closed the first agent's fact."""
    remember(db, tenant, "private", agent="agent-a", when=day(6))
    remember(db, tenant, "suspended", agent="agent-b", when=day(7))
    assert open_values(db, tenant) == {"private": "agent-a", "suspended": "agent-b"}
    assert all("private" not in cands for _new, cands in shown), shown


def test_the_same_agents_fact_is_still_closed(db, tenant, shown, judged):
    """Holds on both: the agent of the write reaches every door of the judge
    (a door that passed none would look among the facts with no agent)."""
    remember(db, tenant, "private", agent="agent-b", when=day(6))
    remember(db, tenant, "suspended", agent="agent-b", when=day(7))
    assert open_values(db, tenant) == {"suspended": "agent-b"}


def test_new_a_write_with_no_agent_meets_only_the_facts_with_no_agent(db, tenant, shown, judged):
    """New. The first choice: the default namespace is one of its own."""
    remember(db, tenant, "private", agent="agent-a", when=day(5))
    remember(db, tenant, "public", agent=None, when=day(6))
    remember(db, tenant, "suspended", agent=None, when=day(7))
    assert open_values(db, tenant) == {"private": "agent-a", "suspended": None}


def test_new_an_agents_write_does_not_close_a_fact_with_no_agent(db, tenant, shown, judged):
    """New. The same wall seen from the other side."""
    remember(db, tenant, "private", agent=None, when=day(6))
    remember(db, tenant, "suspended", agent="agent-b", when=day(7))
    assert open_values(db, tenant) == {"private": None, "suspended": "agent-b"}


@pytest.mark.parametrize("second", ["agent-b", None], ids=["another-agent", "no-agent"])
def test_with_an_end_user_every_agent_is_weighed_as_before(db, tenant, shown, judged, second):
    """Holds on both. The second choice: the end user's facts are one
    person's memory, whichever agent heard them."""
    remember(db, tenant, "private", agent="agent-a", user="u-42", when=day(6))
    remember(db, tenant, "suspended", agent=second, user="u-42", when=day(7))
    assert open_values(db, tenant) == {"suspended": second}


# 2. A fact posted directly (POST /v1/facts) ----------------------------------

@pytest.mark.parametrize("second,closes", [("agent-a", True), ("agent-b", False)],
                         ids=["same-agent", "another-agent"])
def test_a_posted_fact_closes_only_its_agents_facts(db, tenant, shown, second, closes):
    """New for another agent (on 6.27.0 it closed); the same agent's holds
    on both."""
    for agent, obj, d in (("agent-a", "private", 6), (second, "suspended", 7)):
        service.write_fact_direct(db, tenant.id, subject=SUBJECT, predicate=RELATION, obj=obj,
                                  agent_id=agent, valid_from=day(d))
    want = {"suspended": second} if closes else {"private": "agent-a", "suspended": second}
    assert open_values(db, tenant) == want


# 3. The late judgement of a vector -------------------------------------------

def _bare(db, tenant, obj, *, agent, when, vector):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=None, agent_id=agent,
                  subject_label="Acme", subject_canonical="acme", subject_type="organization",
                  predicate_raw="on_plan" if vector else "subscribes_to",
                  predicate_canonical="on_plan" if vector else "subscribes_to",
                  object_label=obj, object_canonical=obj.lower(), confidence=0.9,
                  valid_from=when, created_at=when, tense="current",
                  embedding=vector, embedding_status=vectors.DONE if vector else vectors.PENDING)
    db.add(f)
    db.commit()
    return f


@pytest.mark.parametrize("late_agent,asked", [("agent-a", True), ("agent-b", False)],
                         ids=["same-agent", "another-agent"])
def test_a_late_vector_meets_only_its_agents_facts(db, tenant, monkeypatch, late_agent, asked):
    """New for another agent: on 6.27.0 the late vector of one agent's fact
    asked the judge about, and closed, another agent's fact that only
    meaning finds (tests/test_late_vector_judgement.py, the same world)."""
    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr(service, "fact_vector", lambda text: list(VEC))
    monkeypatch.setattr("korely_agent.services.webhooks.emit_event", lambda *a, **k: None)
    seen = []

    def batch(groups, db_, tenant_id, model_name=None):
        seen.extend(g["new_id"] for g in groups)
        return {g["new_id"]: [c.id for c in g["candidates"]] for g in groups}

    monkeypatch.setattr(contra, "_classify_batch", batch)
    t0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
    old = _bare(db, tenant, "Business plan", agent="agent-a", when=t0, vector=list(VEC))
    late = _bare(db, tenant, "Starter plan", agent=late_agent, when=t0 + timedelta(days=30), vector=None)

    assert vectors.process_pending_vectors(db)["processed"] == 1
    db.expire_all()
    assert (seen == [str(late.id)]) is asked, seen
    assert (db.get(AgentFact, old.id).invalid_at is not None) is asked


# 4. The rule itself -----------------------------------------------------------

def test_new_the_batched_judge_is_told_each_facts_agent():
    """New: `worker._new_for_the_batch` carries `agent_id` (on 6.27.0 the
    batched judge knew the end user only)."""
    f = AgentFact(id=uuid.uuid4(), end_user_id=None, agent_id="agent-a", subject_label="s",
                  subject_canonical="s", predicate_raw="p", predicate_canonical="p", object_label="o",
                  object_canonical="o", tense="current", invalid_at=None, observation_count=1)
    mid = uuid.uuid4()
    f.source_memory_id = mid
    (nf,) = worker._new_for_the_batch([(f, [])], mid)
    assert nf["agent_id"] == "agent-a" and nf["end_user_id"] is None


def test_new_the_wall_is_the_agent_only_with_no_end_user():
    assert owner.judged_within_its_agent(None) is True
    assert owner.judged_within_its_agent("u-42") is False
    assert owner.judged_within_its_agent("") is False, "an id of nothing is an end user's namespace (6.4.0)"
    assert str(owner.same_agent_rows(AgentFact.agent_id, None)) == "agent_facts.agent_id IS NULL"
    assert "agent_facts.agent_id = " in str(owner.same_agent_rows(AgentFact.agent_id, "agent-a"))
