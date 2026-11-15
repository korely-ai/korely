"""memory_core.writing: writing a fact, common to the two Agents engines.

Piece 3 (second half) of the plan for one shared engine,
2026-09-28. The behaviour is covered by the cloud tests that go through
`_persist_one` (vocabulary, alias, reconfirmation, time); here are the rules of
order that the common function guarantees to its caller.

No model is called.
"""
# Port notes: on korely-agent's tables (`facts.entities.TABLES`, tenant_id).
from __future__ import annotations


from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.services.facts import entities
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core.store import Relation
from korely_agent.services.memory_core.writing import write_fact


def _write(db, tenant, *, judge, embed, tense="current", defer=False, obj="Acme"):
    return write_fact(
        db, entities.TABLES, entities.memory_of(tenant.id, None, "u1", None),
        columns=dict(tenant_id=tenant.id, project_id=None, end_user_id="u1", agent_id=None, run_id=None),
        relation=Relation(canonical="works_at", is_functional=True, family="employment", example="Acme"),
        is_open=true_at(), embed=embed, judge=judge,
        subject="Maria", subject_type="person", predicate_raw="works at", obj=obj,
        object_is_literal=False, confidence=0.9, source_memory_id=None, model_used=None,
        tense=tense, defer=defer,
    )


def test_the_vector_comes_before_the_judge_and_the_judge_receives_it(db, tenant):
    order = []

    def embed(f):
        order.append("vector")
        f.embedding = [0.5] * EMBEDDING_DIM

    def judge(g):
        order.append("judge")
        assert g.vector is not None and g.functional is True and g.example == "Acme"
        return []

    fact, closed = _write(db, tenant, judge=judge, embed=embed)
    assert order == ["vector", "judge"] and closed == [] and fact.id is not None


def test_deferred_writes_and_embeds_but_does_not_judge(db, tenant):
    """What the name says, all three (audit of 17 Oct 2026: only the third
    was checked): the fact is in the store, open, with its vector, and the
    judge was never asked."""
    from korely_agent.db.models import AgentFact

    seen, embedded = [], []

    def embed(f):
        embedded.append(f.object_label)
        f.embedding = [0.5] * EMBEDDING_DIM

    fact, closed = _write(db, tenant, judge=lambda g: seen.append(g) or [],
                            embed=embed, defer=True)
    assert seen == [] and closed == []
    assert embedded == ["Acme"]
    db.flush()
    row = db.get(AgentFact, fact.id)
    assert row is not None and row.invalid_at is None and row.embedding is not None


def test_the_same_triple_is_reconfirmed_without_rejudging(db, tenant):
    seen = []
    first, _ = _write(db, tenant, judge=lambda g: [], embed=lambda f: None)
    second, closed = _write(db, tenant, judge=lambda g: seen.append(g) or [],
                              embed=lambda f: seen.append("vector"))
    assert second.id == first.id and second.observation_count == 2 and seen == [] and closed == []


def test_a_past_fact_without_a_twin_is_born_closed_and_asks_only_for_the_arguments(db, tenant):
    seen = []
    fact, _ = _write(db, tenant, tense="past", judge=lambda g: seen.append(g) or [],
                       embed=lambda f: None, obj="Initech")
    assert fact.invalid_at is not None and fact.tense == "past"
    assert len(seen) == 1 and seen[0].past is True and seen[0].vector is None
