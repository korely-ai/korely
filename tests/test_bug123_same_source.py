"""BUG-123 (2026-09-27): a memory that LISTS alternatives does not close itself.

Found on the hosted product: a note saying "Agents plans: hobby, developer,
team, scale" became one fact per plan, and the contradiction judge, reading
`plan` as single-valued, closed hobby with developer, developer with team, and
so on: four facts of the SAME text, each cancelling the one before.

The rule (memory_core.contradiction rule 3; Graphiti, Rasmussen et al. 2025,
arXiv:2501.13956 §2.2.3): a fact never closes a fact stated by the same source,
born from it or reconfirmed by it. The facts of one episode share its time, so
none of them is the newer information. A change told inside one text ("we were
on hobby, now on team") is carried by the tense (`tense=past` is born closed),
and must keep working.

The judge here is fake and at its worst: it closes EVERYTHING it is shown, the
way the real one does with the [single-valued] tag. A fact that stays open
never reached it.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts import contradiction, service
from korely_agent.services.memory_core.fact_extractor import ExtractedFact



class _Judge:
    def __init__(self):
        self.seen = []

    def __call__(self, candidates, *a, **k):
        self.seen.append([c.object_label for c in candidates])
        return [c.id for c in candidates], {}

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
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="acme", content=text)
    db.add(m)
    db.commit()
    return m


def _f(obj, tense="current"):
    return ExtractedFact(subject="Acme", predicate_raw="on_plan", object=obj,
                         confidence=0.9, subject_type="organization", tense=tense)


def _write(db, tenant, memory, facts):
    return service._persist_extracted(db, tenant.id, memory=memory, extracted=facts)


def _open(db):
    return {f.object_label for f in db.query(AgentFact).filter(AgentFact.invalid_at.is_(None))}


def _closed(db):
    return {f.object_label for f in db.query(AgentFact).filter(AgentFact.invalid_at.isnot(None))}


def test_a_memory_listing_the_plans_keeps_them_all_open(db, tenant, judge):
    m = _memory(db, tenant, "Agents plans: hobby, developer, team, scale")
    out = _write(db, tenant, m, [_f("hobby"), _f("developer"), _f("team"), _f("scale")])
    assert len(out) == 4 and all(not o["invalidated"] for o in out)
    assert _open(db) == {"hobby", "developer", "team", "scale"}
    assert all(seen == [] for seen in judge.seen)


def test_a_later_memory_changing_the_plan_still_closes_the_old_one(db, tenant, judge):
    m1 = _memory(db, tenant, "Acme is on hobby")
    m2 = _memory(db, tenant, "Acme moved to developer")
    _write(db, tenant, m1, [_f("hobby")])
    _write(db, tenant, m2, [_f("developer")])
    assert judge.seen[-1] == ["hobby"]
    assert _open(db) == {"developer"} and _closed(db) == {"hobby"}


def test_past_and_present_in_the_same_memory(db, tenant, judge):
    m = _memory(db, tenant, "Acme was on hobby, now on team")
    _write(db, tenant, m, [_f("hobby", tense="past"), _f("team")])
    assert _open(db) == {"team"} and _closed(db) == {"hobby"}


@pytest.mark.parametrize("order", ["past_first", "present_first"])
def test_past_and_present_close_a_fact_from_another_memory(db, tenant, judge, order):
    m1 = _memory(db, tenant, "Acme is on hobby")
    m2 = _memory(db, tenant, "Acme was on hobby, now on team")
    _write(db, tenant, m1, [_f("hobby")])
    facts = [_f("hobby", tense="past"), _f("team")]
    if order == "present_first":
        facts.reverse()
    _write(db, tenant, m2, facts)
    assert _open(db) == {"team"} and _closed(db) == {"hobby"}


def test_a_fact_the_memory_reconfirmed_is_not_closed(db, tenant, judge):
    """m1 says hobby; m2 lists "hobby, developer". hobby now has two sources and
    m2 is one of them: m2 says it holds, it does not close it. The JSON list of
    sources, not only the column of the first one."""
    m1 = _memory(db, tenant, "Acme is on hobby")
    m2 = _memory(db, tenant, "Acme plans: hobby, developer")
    _write(db, tenant, m1, [_f("hobby")])
    _write(db, tenant, m2, [_f("hobby"), _f("developer")])
    hobby = db.query(AgentFact).filter(AgentFact.object_label == "hobby").one()
    assert hobby.observation_count == 2 and str(m2.id) in hobby.source_memory_ids
    assert _open(db) == {"hobby", "developer"}


def test_the_limit_counts_only_real_candidates(db, tenant):
    """A memory listing more alternatives than the LIMIT must not fill it with
    its own facts: the filter is in the query (Postgres, JSON text), not after."""
    old = _memory(db, tenant, "Acme is on legacy")
    lst = _memory(db, tenant, "twelve plans")
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def row(obj, mem, when):
        return AgentFact(
            id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="acme",
            subject_label="Acme", subject_canonical="Acme", predicate_raw="on_plan",
            predicate_canonical="on_plan", object_label=obj, object_canonical=obj,
            confidence=0.9, source_memory_id=mem.id, source_memory_ids=[str(mem.id)],
            valid_from=when, created_at=when,
        )
    db.add(row("legacy", old, t0))
    mine = [row(f"P{i}", lst, t0 + timedelta(days=i + 1)) for i in range(12)]
    db.add_all(mine)
    db.commit()
    got = contradiction._stage1_candidates(
        db, tenant.id, "acme", "Acme", "on_plan",
        exclude_id=mine[-1].id, limit=10, subject_canonical="Acme", object_canonical="P11",
        source=lst.id, agent_id=None,
    )
    assert [c.object_label for c in got] == ["legacy"]
