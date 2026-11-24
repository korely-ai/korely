"""The review of the subjects a memory with no end user holds (memory_core
6.20.0, `objects.review_subjects`; services/facts/subjects.review): its dry
run calls no model and writes nothing, every pair is seen once, and a short
form born first is offered to the longer name born after it. With the model,
the run still writes nothing: its proposals are what `apply` writes."""
from korely_agent.core import llm
from korely_agent.services.facts import subjects
from tests.subject_world import Port, fact, memory, snapshot, vec


def team(db, tenant):
    """"Cloud" born first, "Korely Cloud" after it, an unrelated server."""
    m1 = memory(db, tenant, "Cloud now runs version 1.0.27 on Hetzner.", days_ago=5)
    m2 = memory(db, tenant, "Korely Cloud has version 1.0.30, the Cloud of Korely.", days_ago=2)
    m3 = memory(db, tenant, "The Hetzner box hosts the database.", days_ago=1)
    fact(db, tenant, "Cloud", "runs_version", "1.0.27", mem=m1, minutes_ago=5000, vector=vec(7))
    fact(db, tenant, "Korely Cloud", "has_version", "1.0.30", mem=m2, minutes_ago=2000, vector=vec(7, (8, 0.1)))
    fact(db, tenant, "Hetzner box", "hosts", "database", mem=m3, minutes_ago=1000, vector=vec(300),
         typ="organization")
    return m1, m2, m3


def test_the_dry_run_calls_no_model_and_writes_nothing(db, tenant, monkeypatch):
    team(db, tenant)
    monkeypatch.setattr(llm, "decisions_provider", lambda *a, **k: (_ for _ in ()).throw(AssertionError(
        "a dry run built a model")))
    before = snapshot(db, tenant)
    r = subjects.review(db, tenant.id, None, dry_run=True)
    db.rollback()
    assert snapshot(db, tenant) == before
    assert r["subjects"] == 3
    names = [(p.name, p.candidate) for p in r["pairs"]]
    assert ("Korely Cloud", "Cloud") in names, "the short form born first is offered to the later name"
    assert ("Cloud", "Korely Cloud") not in names, "every pair once: the later name asks"
    [p] = [p for p in r["pairs"] if (p.name, p.candidate) == ("Korely Cloud", "Cloud")]
    assert p.facts == 1 and p.candidate_facts == 1 and p.distance is not None and p.distance < 0.01
    assert r["questions"] >= 1 and r["characters"] > 0
    assert r["proposals"] == [] and r["asked"] == 0


def test_with_the_model_the_run_proposes_and_writes_nothing(db, tenant, monkeypatch):
    team(db, tenant)
    port = Port("1", "same", "none", "none")
    monkeypatch.setattr(llm, "decisions_provider", lambda region: port)
    before = snapshot(db, tenant)
    r = subjects.review(db, tenant.id, None, dry_run=False)
    db.rollback()
    assert snapshot(db, tenant) == before, "a run writes nothing: --apply does"
    assert [(p.name, p.canonical) for p in r["proposals"]] == [("Korely Cloud", "Cloud")]
    state, _instructions, _criteria = port.calls[0]
    assert state["B"]["name"] == "Korely Cloud"
    assert state["B"]["memory_words"].startswith("Korely Cloud has version 1.0.30")
    assert state["names_used_before"]["1"]["facts"] == ["Cloud | runs version | 1.0.27"]


def test_apply_writes_exactly_the_proposals_in_the_wall(db, tenant, monkeypatch):
    from korely_agent.db.models import AgentObjectLink

    team(db, tenant)
    port = Port("1", "same")
    monkeypatch.setattr(llm, "decisions_provider", lambda region: port)
    r = subjects.review(db, tenant.id, None, dry_run=False)
    db.rollback()
    records = [subjects.proposal_record(tenant.id, None, p) for p in r["proposals"]]
    out = subjects.apply(db, records)
    db.commit()
    assert [o["outcome"] for o in out] == ["created"]
    [row] = db.query(AgentObjectLink).all()
    assert (row.name_label, row.canonical_label, row.scope, row.end_user_id, row.agent_id, row.decided_by) == \
        ("Korely Cloud", "Cloud", "wall", None, None, "subject_review")
    assert row.reason["readings"][1] == {"framing": "pair", "verdict": "same"}
    again = subjects.apply(db, records)
    assert [o["outcome"] for o in again] == ["same"], "applied twice, one link"
