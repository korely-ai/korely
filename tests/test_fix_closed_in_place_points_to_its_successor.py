"""A fact closed by a past statement points to the fact that replaced it (E3
of the engine list, 4 Nov 2026; Notion rows, in English: "invalidated_by is
inconsistent: a pointer only when the judge closes" and "a closing in place
... with no invalidated_by pointer").

"The plan costs 55, it used to cost 50": the judge closes a fact with
`invalidated_by_id` = the fact that closed it, but a past statement closes
its row in place (`writing._the_past`), or writes it born closed, and the
pointer stayed empty: whoever read the history saw a hole. Now the current
fact of the same write, about the same subject and relation, is the pointer
(Graphiti, Rasmussen et al. 2025, section 2.2.3: an invalidated edge keeps the
edge that invalidated it). Nothing is judged and nothing else closes.
"""
import pytest

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts import service as fsvc
from korely_agent.services.memory_core.fact_extractor import ExtractedFact
from tests.conftest import stub_extraction


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    monkeypatch.setattr(fsvc, "_embed_fact", lambda *a, **k: None)
    monkeypatch.setattr(fsvc, "detect_and_invalidate", lambda *a, **k: [])


def says(monkeypatch, *facts):
    stub_extraction(monkeypatch, fsvc, lambda *a, **k: [
        ExtractedFact(s, p, o, 0.9, subject_type="thing", tense=t) for s, p, o, t in facts])


def memory(db, tenant, text):
    m = AgentMemory(tenant_id=tenant.id, end_user_id="u1", content=text, graph_sync_status="pending")
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def by_object(db):
    db.expire_all()
    return {f.object_label: f for f in db.query(AgentFact)}


def test_a_past_fact_born_closed_points_to_the_current_one_of_its_write(db, tenant, monkeypatch):
    says(monkeypatch, ("Northwind", "costs", "55", "current"), ("Northwind", "costs", "50", "past"))
    m = memory(db, tenant, "Northwind costs 55 now, it used to cost 50.")
    fsvc.extract_and_persist_facts(db, tenant.id, memory=m)
    facts = by_object(db)
    assert facts["50"].invalid_at is not None and facts["55"].invalid_at is None
    assert facts["50"].invalidated_by_id == facts["55"].id


def test_a_fact_closed_in_place_points_to_the_current_one_of_the_write(db, tenant, monkeypatch):
    says(monkeypatch, ("Northwind", "costs", "50", "current"))
    first = memory(db, tenant, "Northwind costs 50.")
    fsvc.extract_and_persist_facts(db, tenant.id, memory=first)
    says(monkeypatch, ("Northwind", "costs", "55", "current"), ("Northwind", "costs", "50", "past"))
    second = memory(db, tenant, "Northwind costs 55 now, it used to cost 50.")
    fsvc.extract_and_persist_facts(db, tenant.id, memory=second)
    facts = by_object(db)
    assert facts["50"].invalid_at is not None
    assert facts["50"].invalidated_by_id == facts["55"].id


def test_a_past_fact_with_no_current_one_in_its_write_points_nowhere(db, tenant, monkeypatch):
    says(monkeypatch, ("Northwind", "costs", "50", "past"))
    m = memory(db, tenant, "Northwind used to cost 50.")
    fsvc.extract_and_persist_facts(db, tenant.id, memory=m)
    assert by_object(db)["50"].invalidated_by_id is None


def test_a_current_fact_of_another_subject_is_not_the_pointer(db, tenant, monkeypatch):
    says(monkeypatch, ("Contoso", "costs", "70", "current"), ("Northwind", "costs", "50", "past"))
    m = memory(db, tenant, "Contoso costs 70. Northwind used to cost 50.")
    fsvc.extract_and_persist_facts(db, tenant.id, memory=m)
    assert by_object(db)["50"].invalidated_by_id is None


def test_forgetting_the_past_statement_gives_the_old_value_back_as_current(db, tenant, monkeypatch):
    """E3 and the deletion: "Northwind is on the Basic plan", then "moved to
    the Pro plan, it used to be on the Basic plan". The second forgotten, the
    first still says Basic: it is true again, as a new open row of tense
    'current' (not 'past': an open row is not history)."""
    from korely_agent.services.facts import erasure

    says(monkeypatch, ("Northwind", "uses", "Basic plan", "current"))
    first = memory(db, tenant, "Northwind is on the Basic plan.")
    fsvc.extract_and_persist_facts(db, tenant.id, memory=first)
    says(monkeypatch, ("Northwind", "uses", "Pro plan", "current"), ("Northwind", "uses", "Basic plan", "past"))
    second = memory(db, tenant, "Northwind moved to the Pro plan, it used to be on the Basic plan.")
    fsvc.extract_and_persist_facts(db, tenant.id, memory=second)

    erasure.forget_memory(db, tenant.id, second.id, project_id=second.project_id)
    db.commit()
    db.expire_all()
    basic = [f for f in db.query(AgentFact).filter(AgentFact.object_label == "Basic plan") if f.invalid_at is None]
    assert len(basic) == 1 and basic[0].tense == "current"
