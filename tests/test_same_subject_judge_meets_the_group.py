"""With a link between two subjects of the wall (memory_core 6.20.0), the judge
of a write with no end user weighs the new fact against the facts of the
other names of its subject's group, and records each closing it made only so
on the link; with no link, with an end user, or for a subject the caller
named, nothing changes."""
import pytest

from korely_agent.db.models import AgentFact, AgentFactClosureLink
from korely_agent.services.facts import contradiction, subjects
from tests.subject_world import Judge, fact, memory, vec, wall_link


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(subjects, "SAME_SUBJECT_REVIEW", True)


@pytest.fixture
def judge(monkeypatch):
    j = Judge()
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: j)
    return j


def cloud(db, tenant, *, user=None, other="ops-agent"):
    """One agent: "Korely Cloud has_version 1.0.30" (old), and the new
    "Cloud di Korely runs_version 1.0.35", near by meaning. Until memory_core
    6.28.0 the new fact was another agent's; since then, with no end user,
    the judge weighs a fact only against its own agent's (`other`: the
    agent of the new fact, `owner.judged_within_its_agent`)."""
    a = memory(db, tenant, "Korely Cloud has version 1.0.30.", agent="ops-agent", days_ago=5)
    b = memory(db, tenant, "Il Cloud di Korely gira la 1.0.35.", agent=other, days_ago=1)
    old = fact(db, tenant, "Korely Cloud", "has_version", "1.0.30", mem=a, agent="ops-agent", minutes_ago=3000,
               valid_days_ago=5, vector=vec(5), user=user)
    new = fact(db, tenant, "Cloud di Korely", "runs_version", "1.0.35", mem=b, agent=other,
               minutes_ago=1, valid_days_ago=1, vector=vec(5, (6, 0.05)), user=user)
    return old, new, a, b


def judged(db, tenant, new, *, user=None, subject_named=False):
    out = contradiction.detect_and_invalidate(
        db, tenant.id, end_user_id=user, new_fact_id=new.id, new_subject=new.subject_label,
        new_predicate_raw=new.predicate_raw, new_predicate_canonical=new.predicate_canonical,
        new_object=new.object_label, new_embedding=new.embedding, new_valid_from=new.valid_from,
        new_subject_canonical=new.subject_canonical, new_object_canonical=new.object_canonical,
        source=new.source_memory_id, subject_named=subject_named, agent_id=new.agent_id)
    db.commit()
    return out


def test_the_group_brings_the_other_names_fact_and_the_closing_is_recorded(db, tenant, on, judge):
    old, new, a, _b = cloud(db, tenant)
    link = wall_link(db, tenant, "Korely Cloud", "Cloud di Korely", source=a)
    assert judged(db, tenant, new) == [old.id]
    db.expire_all()
    closed = db.get(AgentFact, old.id)
    assert closed.invalid_at is not None and closed.invalidated_by_id == new.id
    assert [(r.fact_id, r.link_id) for r in db.query(AgentFactClosureLink)] == [(old.id, link.id)]


def test_without_a_link_the_other_name_is_not_a_candidate(db, tenant, on, judge):
    old, new, _a, _b = cloud(db, tenant)
    assert judged(db, tenant, new) == []
    assert judge.prompts == [], "no candidate, no call"
    assert db.query(AgentFactClosureLink).count() == 0


def test_a_closing_by_the_names_own_facts_is_not_recorded_on_the_link(db, tenant, on, judge):
    old, new, a, _b = cloud(db, tenant)
    wall_link(db, tenant, "Korely Cloud", "Cloud di Korely", source=a)
    c = memory(db, tenant, "Cloud di Korely runs 1.0.34.", agent="ops-agent", days_ago=3)
    own = fact(db, tenant, "Cloud di Korely", "runs_version", "1.0.34", mem=c, agent="ops-agent",
               minutes_ago=2000, valid_days_ago=3, vector=vec(5))
    assert sorted(judged(db, tenant, new), key=str) == sorted([old.id, own.id], key=str)
    assert [r.fact_id for r in db.query(AgentFactClosureLink)] == [old.id], \
        "only the closing the link made possible is the link's"


def test_the_new_fact_closed_by_a_later_one_of_the_other_name_is_recorded(db, tenant, on, judge):
    """The judge closes the new fact when the candidate began later
    (memory_core `judge.close`): that closing is the link's too."""
    a = memory(db, tenant, "Korely Cloud runs 1.0.35 since yesterday.", agent="ops-agent", days_ago=5)
    later = fact(db, tenant, "Korely Cloud", "runs_version", "1.0.35", mem=a, agent="ops-agent",
                 minutes_ago=3000, valid_days_ago=1, vector=vec(5))
    b = memory(db, tenant, "Il Cloud di Korely aveva la 1.0.30.", agent="ops-agent", days_ago=1)
    earlier = fact(db, tenant, "Cloud di Korely", "runs_version", "1.0.30", mem=b, agent="ops-agent",
                   minutes_ago=1, valid_days_ago=9, vector=vec(5))
    link = wall_link(db, tenant, "Korely Cloud", "Cloud di Korely", source=a)
    assert judged(db, tenant, earlier) == []
    db.expire_all()
    assert db.get(AgentFact, earlier.id).invalidated_by_id == later.id
    assert [(r.fact_id, r.link_id) for r in db.query(AgentFactClosureLink)] == [(earlier.id, link.id)]


def test_with_an_end_user_nothing_changes(db, tenant, on, judge):
    """A memory with an end user is not a wall: its judge already weighs
    other subjects (6.6.0), and no link of a wall is read for it."""
    old, new, a, _b = cloud(db, tenant, user="maria")
    wall_link(db, tenant, "Korely Cloud", "Cloud di Korely", source=a)
    base = contradiction._stage1_candidates(
        db, tenant.id, "maria", new.subject_label, new.predicate_canonical, exclude_id=new.id,
        new_embedding=new.embedding, subject_canonical=new.subject_canonical,
        object_canonical=new.object_canonical, source=new.source_memory_id, agent_id=new.agent_id)
    extra, through = subjects.extra_candidates(
        db, tenant.id, None, "maria", new.subject_label, new.subject_canonical, base,
        lambda names: pytest.fail("a group read for a memory with an end user"))
    assert (extra, through) == ([], {})
    judged(db, tenant, new, user="maria")
    assert db.query(AgentFactClosureLink).count() == 0


def test_a_subject_the_caller_named_meets_its_own_facts_only(db, tenant, on, judge):
    old, new, a, _b = cloud(db, tenant)
    wall_link(db, tenant, "Korely Cloud", "Cloud di Korely", source=a)
    assert judged(db, tenant, new, subject_named=True) == []
    assert db.query(AgentFactClosureLink).count() == 0


def test_the_batched_judge_records_the_same_closing(db, tenant, on, judge):
    old, new, a, _b = cloud(db, tenant)
    link = wall_link(db, tenant, "Korely Cloud", "Cloud di Korely", source=a)
    nf = {"id": new.id, "end_user_id": None, "subject": new.subject_label,
          "subject_canonical": new.subject_canonical, "predicate_raw": new.predicate_raw,
          "predicate_canonical": new.predicate_canonical, "object": new.object_label,
          "object_canonical": new.object_canonical, "embedding": new.embedding,
          "valid_from": new.valid_from, "source_memory_id": new.source_memory_id,
          "agent_id": new.agent_id}
    closed = contradiction.detect_and_invalidate_batch(db, tenant.id, new_facts=[nf])
    db.commit()
    assert closed == {new.id: [old.id]}
    assert [(r.fact_id, r.link_id) for r in db.query(AgentFactClosureLink)] == [(old.id, link.id)]


def test_another_agents_fact_of_the_group_is_not_a_candidate(db, tenant, on, judge):
    """memory_core 6.28.0 (a product decision of 26 Nov 2026): with no
    end user a link between two names unites them inside one agent's facts,
    never across agents; on 6.27.0 the planner's write closed the ops
    agent's fact through the link."""
    old, new, a, _b = cloud(db, tenant, other="planner")
    wall_link(db, tenant, "Korely Cloud", "Cloud di Korely", source=a)
    assert judged(db, tenant, new) == []
    assert db.get(AgentFact, old.id).invalid_at is None
    assert db.query(AgentFactClosureLink).count() == 0
