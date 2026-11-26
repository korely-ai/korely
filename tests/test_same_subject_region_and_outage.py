"""The review of subjects asks only in the Global region, never falls back,
and a provider that does not answer defers it (memory_core 6.20.0,
services/facts/subjects.review): nothing is written, no write waits for it,
and no lock is held while the model is asked."""
import pytest
from sqlalchemy import text

from korely_agent.core import llm
from korely_agent.services.facts import subjects
from tests.subject_world import Port, fact, memory, snapshot, vec


def team(db, tenant):
    m1 = memory(db, tenant, "Cloud now runs 1.0.27.", days_ago=5)
    m2 = memory(db, tenant, "Korely Cloud has version 1.0.30.", days_ago=2)
    fact(db, tenant, "Cloud", "runs_version", "1.0.27", mem=m1, minutes_ago=5000, vector=vec(7))
    fact(db, tenant, "Korely Cloud", "has_version", "1.0.30", mem=m2, minutes_ago=2000, vector=vec(7))


def test_a_european_project_is_never_asked(db, tenant, monkeypatch):
    from korely_agent.services import regions

    team(db, tenant)
    monkeypatch.setattr(regions, "of_project", lambda *a, **k: "eu")
    monkeypatch.setattr(llm.get_settings(), "openrouter_api_key", "sk-or-test")
    with pytest.raises(subjects.RegionRefused):
        subjects.review(db, tenant.id, None, dry_run=False)
    assert llm.decisions_provider("eu") is None
    with pytest.raises(llm.RegionRefused):
        llm.DecisionsProvider("sk-or-test", model="typesafe/jev-1.13", region="eu")
    assert subjects.review(db, tenant.id, None, dry_run=True)["questions"] == 1, "the dry run still runs"


def test_no_key_no_model(db, tenant, monkeypatch):
    monkeypatch.setattr(llm.get_settings(), "openrouter_api_key", None)
    assert llm.decisions_provider("global") is None


@pytest.mark.parametrize("status", [402, 429, None], ids=["402", "429", "down"])
def test_a_provider_that_does_not_answer_defers_the_run(db, tenant, monkeypatch, status):
    team(db, tenant)

    def refuse(state, instructions, criteria):
        if status is None:
            raise ConnectionError("unreachable")
        import urllib.error
        raise urllib.error.HTTPError("https://openrouter.ai/api/alpha/decisions", status, "no", {}, None)
    monkeypatch.setattr(llm, "decisions_provider", lambda region: Port(refuse))
    before = snapshot(db, tenant)
    r = subjects.review(db, tenant.id, None, dry_run=False)
    db.rollback()
    assert r["deferred"] and r["proposals"] == [] and r["asked"] == 0
    assert snapshot(db, tenant) == before


def test_no_lock_is_held_while_the_model_is_asked(db, tenant, monkeypatch):
    """Every read of the run comes first, and its transaction ends before
    each question: no advisory lock, and no lock of any table of the store,
    not even the ACCESS SHARE of a read (the review of 23 Nov 2026, P7)."""
    team(db, tenant)
    held = []

    def look(state, instructions, criteria):
        held.extend(db.execute(text(
            "SELECT coalesce(c.relname, l.locktype) FROM pg_locks l LEFT JOIN pg_class c ON c.oid = l.relation "
            "WHERE l.pid = pg_backend_pid() AND (l.locktype = 'advisory' OR c.relname LIKE 'agent_%')")).scalars())
        return "none"
    monkeypatch.setattr(llm, "decisions_provider", lambda region: Port(look))
    r = subjects.review(db, tenant.id, None, dry_run=False)
    db.rollback()
    assert r["asked"] == 1
    assert held == []


def test_a_write_never_waits_for_the_decisions_model(db, tenant, monkeypatch):
    """With the switch on and a link in the wall, the judge of a write with
    no end user reads the link and never the decisions model: the review is
    a run of its own, and a provider down there stops no write."""
    from korely_agent.services.facts import contradiction
    from tests.subject_world import Judge, wall_link

    monkeypatch.setattr(subjects, "SAME_SUBJECT_REVIEW", True)
    monkeypatch.setattr(llm, "decisions_provider", lambda region: pytest.fail("a write asked the decisions model"))
    monkeypatch.setattr(llm.DecisionsProvider, "choose", lambda *a: pytest.fail("a write asked the decisions model"))
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: Judge())
    m1 = memory(db, tenant, "Korely Cloud has 1.0.30.", agent="ops-agent", days_ago=4)
    m2 = memory(db, tenant, "Cloud di Korely runs 1.0.35.", agent="ops-agent", days_ago=1)
    old = fact(db, tenant, "Korely Cloud", "has_version", "1.0.30", mem=m1, agent="ops-agent", minutes_ago=3000,
               valid_days_ago=4, vector=vec(9))
    new = fact(db, tenant, "Cloud di Korely", "runs_version", "1.0.35", mem=m2, agent="ops-agent", minutes_ago=1,
               valid_days_ago=1, vector=vec(9))
    wall_link(db, tenant, "Korely Cloud", "Cloud di Korely", source=m1)
    assert contradiction.detect_and_invalidate(
        db, tenant.id, end_user_id=None, new_fact_id=new.id, new_subject=new.subject_label,
        new_predicate_raw=new.predicate_raw, new_predicate_canonical=new.predicate_canonical,
        new_object=new.object_label, new_embedding=new.embedding, new_valid_from=new.valid_from,
        new_subject_canonical=new.subject_canonical, new_object_canonical=new.object_canonical,
        source=new.source_memory_id, agent_id=new.agent_id) == [old.id]
