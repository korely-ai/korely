"""memory_core 2.12.0: the automatic alias without the confidence threshold.

The decision of 6 October (code audit): an explicit identity fact
created the alias by itself only with a declared confidence of 0.9 or more.
Xiong et al. (ICLR 2024): the confidence a model declares sits between 80% and
100% even when it is wrong. The categorical criteria stay: one of the four
identity relations, a product or an organization, two names, different.
Never `replaces`, never similarity.

No model call.
"""
# Port notes: the write runs on korely-agent's tables (AgentMemory, AgentFact,
# AgentEntityAlias with tenant_id, `facts.entities.TABLES`) instead of the
# hosted product's (user_id).
from __future__ import annotations


from korely_agent.db.models import AgentEntityAlias, AgentMemory
from korely_agent.services.facts import entities
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import extraction, identity
from korely_agent.services.memory_core.store import Relation
from korely_agent.services.memory_core.writing import write_fact


def test_the_rule_does_not_read_the_confidence():
    for c in (None, 0.0, 0.3, 0.89, 0.9, 1.0):
        assert identity.alias_rule_from_identity("renamed_to", "Acme", "Globex", subject_type="organization",
                                                 confidence=c) == ("Acme", "Globex")
    assert not hasattr(identity, "AUTOMATIC_ALIAS_CONFIDENCE_THRESHOLD")


def test_the_categorical_criteria_stay():
    r = identity.alias_rule_from_identity
    assert r("replaces", "Deepgram", "Soniox", subject_type="product", confidence=1.0) is None
    assert r("renamed_to", "Marco", "Mario", subject_type="person", confidence=1.0) is None
    assert r("works_at", "Acme", "Globex", subject_type="organization", confidence=1.0) is None
    assert r("renamed_to", "Acme", "acme", subject_type="organization", confidence=1.0) is None
    assert r("formerly", "Globex", "Acme", subject_type="organization") == ("Acme", "Globex")


def test_the_identity_prompt_stays_the_measured_one():
    """The prompt rule stays byte for byte (the fingerprints of
    test_memory_core_observation): the model still writes a confidence, which
    is kept on the row and no longer decides anything. Removing it from the
    prompt is a change to measure, not part of this decision."""
    assert "confidence 0.9+" in extraction.RULES_IDENTITY
    assert "renamed_to" in extraction.RULES_IDENTITY


def test_a_rename_with_low_confidence_creates_the_alias_in_agents(db, tenant):
    """Before: with confidence 0.5 the fact was written and the alias was not,
    and "Acme" and "Globex" stayed two entities for the memory."""
    m = AgentMemory(tenant_id=tenant.id, end_user_id="u1", content="Acme is now called Globex.",
                    graph_sync_status="done")
    db.add(m)
    db.commit()
    write_fact(
        db, entities.TABLES, entities.memory_of(tenant.id, None, "u1", None),
        columns=dict(tenant_id=tenant.id, project_id=None, end_user_id="u1", agent_id=None, run_id=None),
        relation=Relation(canonical="renamed_to", is_functional=None, family="identity"),
        is_open=true_at(), embed=lambda f: None, judge=lambda g: [],
        subject="Acme", subject_type="organization", predicate_raw="renamed_to", obj="Globex",
        object_is_literal=False, confidence=0.5, source_memory_id=m.id, model_used=None,
    )
    alias = db.query(AgentEntityAlias).filter(AgentEntityAlias.tenant_id == tenant.id).all()
    assert [(a.alias_lower, a.canonical_label) for a in alias] == [("acme", "Globex")]
