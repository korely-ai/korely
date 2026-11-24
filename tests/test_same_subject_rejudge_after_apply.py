"""After `apply` (memory_core 6.20.0, services/facts/subjects.rejudge): the open
facts of the names just united are judged against the earlier facts of the
other names, the judgement their writes would have made with the link, and
each closing is recorded on the link. Fact by fact, each committed alone; a
judge that gives no answer stops it and keeps what was done."""
import pytest

from korely_agent.db.models import AgentFact, AgentFactClosureLink, AgentObjectLink
from korely_agent.services.facts import subjects
from korely_agent.services.memory_core import objects as O
from tests.subject_world import Judge, fact, memory, vec


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(subjects, "SAME_SUBJECT_REVIEW", True)


def four_names(db, tenant):
    """The team's Cloud: the old version facts on three names, the current
    one on the fourth."""
    m = {n: memory(db, tenant, f"{n} notes", agent=a, days_ago=d)
         for n, a, d in (("Il Cloud di Korely", "ops-agent", 9), ("Cloud", "planner", 7),
                         ("Korely Cloud", "docs-agent", 4), ("Cloud di Korely", "planner", 1))}
    old = [fact(db, tenant, "Il Cloud di Korely", "runs_version", "1.0.12", mem=m["Il Cloud di Korely"],
                agent="ops-agent", minutes_ago=9000, valid_days_ago=9, vector=vec(5)),
           fact(db, tenant, "Cloud", "exists_in_version", "1.0.27", mem=m["Cloud"], agent="planner",
                minutes_ago=7000, valid_days_ago=7, vector=vec(5)),
           fact(db, tenant, "Korely Cloud", "exists_in_version", "1.0.30", mem=m["Korely Cloud"], agent="docs-agent",
                minutes_ago=4000, valid_days_ago=4, vector=vec(5))]
    now = fact(db, tenant, "Cloud di Korely", "runs_version", "1.0.35", mem=m["Cloud di Korely"],
               agent="planner", minutes_ago=100, valid_days_ago=1, vector=vec(5))
    return old, now, m


def applied(db, tenant, pairs, m):
    records = [subjects.proposal_record(tenant.id, None, O.Proposal(
        name=n, canonical=c, source_memory_id=m[n].id, source_fact_id=None,
        canonical_memory_id=m[c].id)) for n, c in pairs]
    out = subjects.apply(db, records)
    db.commit()
    return out


def test_the_old_versions_of_the_other_names_close_and_are_recorded(db, tenant, monkeypatch, on):
    judge = Judge(closes=lambda line: "1.0.35" not in line)
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: judge)
    old, now, m = four_names(db, tenant)
    out = applied(db, tenant, [("Il Cloud di Korely", "Cloud di Korely"), ("Cloud", "Cloud di Korely"),
                               ("Korely Cloud", "Cloud di Korely")], m)
    assert [o["outcome"] for o in out] == ["created"] * 3
    r = subjects.rejudge(db, tenant.id, None, "Cloud di Korely")
    assert r["deferred"] is None and r["facts"] == 4
    db.expire_all()
    for f in old:
        assert db.get(AgentFact, f.id).invalid_at is not None, f.object_label
    assert db.get(AgentFact, now.id).invalid_at is None
    links = {row.name_label: row.id for row in db.query(AgentObjectLink)}
    recorded = {(r.fact_id, r.link_id) for r in db.query(AgentFactClosureLink)}
    assert {f for f, _l in recorded} == {f.id for f in old}
    assert (old[0].id, links["Il Cloud di Korely"]) in recorded


def test_a_judge_that_does_not_answer_defers_and_keeps_what_was_done(db, tenant, monkeypatch, on):
    class Down:
        def complete(self, *a, **k):
            raise RuntimeError("503")
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: Down())
    old, now, m = four_names(db, tenant)
    applied(db, tenant, [("Korely Cloud", "Cloud di Korely")], m)
    r = subjects.rejudge(db, tenant.id, None, "Cloud di Korely")
    assert r["deferred"] and r["judged"] == 0
    db.expire_all()
    assert all(db.get(AgentFact, f.id).invalid_at is None for f in old + [now])
    assert db.query(AgentObjectLink).count() == 1, "the link stays: apply again runs the judgement again"


def test_off_it_judges_nothing(db, tenant, monkeypatch):
    judge = Judge()
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: judge)
    old, now, m = four_names(db, tenant)
    applied(db, tenant, [("Korely Cloud", "Cloud di Korely")], m)
    assert subjects.rejudge(db, tenant.id, None, "Cloud di Korely")["judged"] == 0
    assert judge.prompts == []


def test_each_fact_is_weighed_against_earlier_facts_only(db, tenant, monkeypatch, on):
    """The judgement a write would have made: a fact against the facts
    written before it (`before=`), so each pair of the group is asked once,
    by its later fact, never also the other way round."""
    judge = Judge(closes=lambda line: False)
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: judge)
    old, now, m = four_names(db, tenant)
    applied(db, tenant, [("Korely Cloud", "Cloud di Korely")], m)
    r = subjects.rejudge(db, tenant.id, None, "Cloud di Korely")
    assert r["judged"] == 1 and len(judge.prompts) == 1, "one pair, one question"
    assert "1.0.30" in judge.prompts[0] and "1.0.35" in judge.prompts[0]


def test_a_file_that_names_another_wall_is_refused(db, tenant, on):
    """The review of 23 Nov 2026, P6: a record naming a memory of another
    account (or project, or of an end user) is refused, no link points into
    it, and that account's erasure of its own memory then passes."""
    import uuid

    from korely_agent.db.models import Tenant
    from korely_agent.services.facts import erasure

    old, now, m = four_names(db, tenant)
    other = Tenant(id=uuid.uuid4(), name="Other")
    db.add(other)
    db.commit()
    theirs = memory(db, other, "Another account's note.", agent="x", days_ago=1)
    rec = subjects.proposal_record(tenant.id, None, O.Proposal(
        name="Korely Cloud", canonical="Cloud di Korely", source_memory_id=m["Korely Cloud"].id,
        source_fact_id=None, canonical_memory_id=theirs.id))
    [res] = subjects.apply(db, [rec])
    db.commit()
    assert (res["outcome"], res["link_id"]) == ("refused", None)
    assert db.query(AgentObjectLink).count() == 0
    assert erasure.erase_memory(db, other.id, theirs.id, project_id=None)["status"] == "erased"


def test_only_what_the_link_brings_is_asked_again(db, tenant, monkeypatch, on):
    """A fact's own name's earlier facts were weighed by its write: the
    judgement after the link asks only the other names' (a candidate of the
    write asked again would be paid twice, and could be answered otherwise)."""
    judge = Judge(closes=lambda line: False)
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: judge)
    a = memory(db, tenant, "Korely Cloud has version 1.0.30.", agent="ops-agent", days_ago=9)
    b = memory(db, tenant, "Cloud di Korely runs 1.0.34.", agent="planner", days_ago=5)
    c = memory(db, tenant, "Cloud di Korely runs 1.0.35.", agent="planner", days_ago=1)
    fact(db, tenant, "Korely Cloud", "has_version", "1.0.30", mem=a, agent="ops-agent", minutes_ago=9000,
         valid_days_ago=9, vector=vec(5))
    fact(db, tenant, "Cloud di Korely", "runs_version", "1.0.34", mem=b, agent="planner", minutes_ago=5000,
         valid_days_ago=5, vector=vec(5))
    fact(db, tenant, "Cloud di Korely", "runs_version", "1.0.35", mem=c, agent="planner", minutes_ago=100,
         valid_days_ago=1, vector=vec(5))
    applied(db, tenant, [("Korely Cloud", "Cloud di Korely")], {"Korely Cloud": a, "Cloud di Korely": c})
    subjects.rejudge(db, tenant.id, None, "Cloud di Korely")
    [latest] = [p for p in judge.prompts if "1.0.35" in p]
    assert "1.0.30" in latest and "1.0.34" not in latest
