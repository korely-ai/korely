"""Once a name is declared for the end user, a namesake's fact never meets theirs (memory_core 6.25.0).

On the identity bench of 17 Oct 2026, "Marco Bianchi eats meat at every meal" closed "the user is
vegetarian" for a user called Marco, 2-3 such closures a run, through the
stages that look past the subject (1a-bis, the same relation inside one end
user; 1b, the nearest facts by meaning). With a name declared for the end
user every name of the person is the end user's id (the aliases of the
write), so a fact about the person and a fact about another subject are two
entities, and the candidates keep them apart, as Graphiti looks for
invalidations only among the edges of the resolved entity
(`candidates.about_the_subject(the_person=)`). Without a declared name
nothing changes. The fakes: one vector for every fact, a judge that records
what it is shown and closes nothing; no model, no network.
"""
import uuid

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory
from korely_agent.services.facts import contradiction as contra
from korely_agent.services.facts import entities as ent
from korely_agent.services.facts import service
from korely_agent.services.memory_core.fact_extractor import ExtractedFact

VEC = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
USER = "marco-v"


@pytest.fixture
def shown(monkeypatch):
    # The rule is off by default (measured on 25 Nov 2026): these tests turn it on.
    monkeypatch.setattr(contra, "_DECLARED_PERSON", True)
    seen = []

    def one(cands, new_subject, new_predicate_raw, new_object, *a, **k):
        seen.append((new_subject, sorted((c.subject_label, c.object_label) for c in cands)))
        return [], {}

    def batch(groups, db, tenant_id, model_name=None):
        from korely_agent.db.models import AgentFact

        for g in groups:
            f = db.get(AgentFact, uuid.UUID(g["new_id"]))
            seen.append((f.subject_label, sorted((c.subject_label, c.object_label) for c in g["candidates"])))
        return {g["new_id"]: [] for g in groups}

    monkeypatch.setattr(service, "fact_vector", lambda text: list(VEC))
    monkeypatch.setattr(contra, "_classify", one)
    monkeypatch.setattr(contra, "_classify_batch", batch)
    monkeypatch.setattr("korely_agent.services.webhooks.emit_event", lambda *a, **k: None)
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
    return seen


def remember(db, tenant, subject, predicate, obj):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=USER, agent_id=None,
                    content=f"{subject} {predicate} {obj}")
    db.add(m)
    db.commit()
    service._persist_extracted(db, tenant.id, memory=m, valid_from=None, extracted=[
        ExtractedFact(subject=subject, predicate_raw=predicate, object=obj, confidence=0.9)])


def candidates_of(shown, subject):
    return [c for s, c in shown if s == subject]


def test_with_a_declared_name_the_namesake_meets_none_of_the_users_facts(db, tenant, shown):
    """Fails on memory_core 6.24.0: the user's diet was shown to the judge."""
    ent.declare_alias(db, tenant.id, None, USER, None, "Marco Verdi", USER, canonical_is_an_end_user=True)
    db.commit()
    remember(db, tenant, USER, "follows_diet", "vegetarian")
    remember(db, tenant, "Marco Bianchi", "follows_diet", "meat at every meal")
    assert candidates_of(shown, "Marco Bianchi") in ([], [[]])
    remember(db, tenant, "Marco Verdi", "follows_diet", "vegan")
    assert [("Marco Bianchi", "meat at every meal")] not in candidates_of(shown, "Marco Verdi")
    assert any((USER, "vegetarian") in c for c in candidates_of(shown, "Marco Verdi")), \
        "a declared name of the person still meets the person's facts"


def test_without_a_declared_name_the_stages_are_as_before(db, tenant, shown):
    remember(db, tenant, USER, "follows_diet", "vegetarian")
    remember(db, tenant, "Marco Bianchi", "follows_diet", "meat at every meal")
    assert any((USER, "vegetarian") in c for c in candidates_of(shown, "Marco Bianchi"))


def test_two_other_subjects_still_meet(db, tenant, shown):
    """The rule keeps the person apart from the others, not the others from
    each other: "Elena" and "Elena Poli" of a third person still meet."""
    ent.declare_alias(db, tenant.id, None, USER, None, "Marco Verdi", USER, canonical_is_an_end_user=True)
    db.commit()
    remember(db, tenant, "Anna", "follows_diet", "keto")
    remember(db, tenant, "Anna Rossi", "follows_diet", "paleo")
    assert any(("Anna", "keto") in c for c in candidates_of(shown, "Anna Rossi"))


def test_the_switch_off_is_the_statements_of_before(db, tenant, shown, monkeypatch):
    monkeypatch.setattr(contra, "_DECLARED_PERSON", False)
    ent.declare_alias(db, tenant.id, None, USER, None, "Marco Verdi", USER, canonical_is_an_end_user=True)
    db.commit()
    remember(db, tenant, USER, "follows_diet", "vegetarian")
    remember(db, tenant, "Marco Bianchi", "follows_diet", "meat at every meal")
    assert any((USER, "vegetarian") in c for c in candidates_of(shown, "Marco Bianchi"))


def test_the_rule_is_off_by_default():
    """Measured on 25 Nov 2026: one right closing lost, no wrong one removed."""
    assert contra._DECLARED_PERSON is False
