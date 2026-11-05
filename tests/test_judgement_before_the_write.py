"""Every judgement of one write weighs the facts as they were before it
(memory_core 3.1.3, piece 6 of the 3.1 plan; 2026-10-06).

The acceptance run of 3.0.0 in the hosted product (11/12 twice): from "Globex moved
to the Starter plan." the extractor wrote the past statement too ("Globex was
on the Business plan"); judged first, it closed the plan, and the next fact
of the same write, "Globex on the Starter plan", was judged against what was
left open: Slack, read as the only subscription, and closed. Zep/Graphiti
resolve all the edges of an episode against the graph as it was before the
episode (`resolve_extracted_edges`): a fact an earlier statement of the same
write closed is still a candidate of the later judgements, and naming it again
closes nothing.

This engine judges one fact at a time (`_persist_one`), through the common
writer (`worker.write_memory`), which opens the write. The judge here is
fake and plays the real one's habit: it closes the candidates that are plans,
and when none is, everything it is shown (the [single-valued] tag).
"""
import uuid

import pytest

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts import contradiction, service
from korely_agent.services.memory_core.fact_extractor import ExtractedFact



class _Judge:
    def __init__(self):
        self.seen = []
        self.watch = None  # called with the candidates, as the judge sees them

    def __call__(self, candidates, *a, **k):
        self.seen.append(sorted(c.object_label for c in candidates))
        if self.watch is not None:
            self.watch(candidates)
        plans = [c.id for c in candidates if c.object_label.endswith("plan")]
        return (plans or [c.id for c in candidates]), {}

    def batch(self, groups, *a, **k):
        """The same judge for a write judged in one call (`_BATCHED_JUDGE`)."""
        return {g["new_id"]: self(g["candidates"])[0] for g in groups}


@pytest.fixture
def judge(monkeypatch):
    j = _Judge()
    monkeypatch.setattr(contradiction, "_classify", j)
    monkeypatch.setattr(contradiction, "_classify_batch", j.batch)
    return j


def _memory(db, tenant, text):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="globex", content=text)
    db.add(m)
    db.commit()
    return m


def _f(obj, tense="current"):
    return ExtractedFact(subject="Globex", predicate_raw="on_plan", object=obj,
                         confidence=0.9, subject_type="organization", tense=tense)


def _write(db, tenant, text, facts):
    return service._persist_extracted(db, tenant.id, memory=_memory(db, tenant, text), extracted=facts)


def _open(db):
    return sorted(f.object_label for f in db.query(AgentFact).filter(AgentFact.invalid_at.is_(None)))


def test_a_plan_change_said_with_its_past_closes_the_plan_and_not_slack(db, tenant, judge):
    _write(db, tenant, "Globex is on the Business plan and subscribes to Slack.",
           [_f("Business plan"), _f("Slack")])
    assert _open(db) == ["Business plan", "Slack"]

    _write(db, tenant, "Globex moved to the Starter plan.",
           [_f("Business plan", tense="past"), _f("Starter plan")])

    assert "Slack" in _open(db), (
        "the plan the past statement closed had left the candidates, and the "
        f"judge saw Slack alone (candidates seen: {judge.seen})")
    assert _open(db) == ["Slack", "Starter plan"]
    assert ["Business plan", "Slack"] in judge.seen, "the new plan is weighed against the plan it replaces"


def test_naming_a_fact_the_write_closed_closes_nothing_more(db, tenant, judge):
    """The closed plan is a candidate again, never reopened nor closed twice:
    its end stays the one the past statement gave it. The end is read when
    the judge is shown the plan, before the judge names it, and again after
    the write: until the audit of 17 Oct 2026 both reads came after the
    write, so a second closing compared equal to itself."""
    _write(db, tenant, "Globex is on the Business plan.", [_f("Business plan")])
    business_id = db.query(AgentFact.id).filter(AgentFact.object_label == "Business plan").scalar()
    ends = []

    def when_judged(candidates):
        for c in candidates:
            if c.id == business_id:
                f = db.get(AgentFact, c.id)
                ends.append((f.invalid_at, f.invalidated_by_id))

    judge.watch = when_judged
    _write(db, tenant, "Globex moved to the Starter plan.",
           [_f("Business plan", tense="past"), _f("Starter plan")])
    assert len(ends) == 1, f"the closed plan was not among the candidates: {judge.seen}"
    assert ends[0][0] is not None, "the plan was still open when the judge named it"

    db.expire_all()
    again = db.get(AgentFact, business_id)
    assert (again.invalid_at, again.invalidated_by_id) == ends[0], "closed a second time"
    assert _open(db) == ["Starter plan"]


def test_outside_a_write_the_candidates_are_what_is_true_now(db, tenant, judge):
    """A fact written on its own (POST /v1/facts) opens no write: nothing it did
    not see before is weighed now."""
    _write(db, tenant, "Globex is on the Business plan.", [_f("Business plan")])
    service.write_fact_direct(db, tenant.id, subject="Globex", predicate="on_plan",
                              obj="Starter plan", end_user_id="globex")
    assert judge.seen[-1] == ["Business plan"]
    assert _open(db) == ["Starter plan"]
