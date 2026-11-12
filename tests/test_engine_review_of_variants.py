"""memory_core 3.3.0: the review of the variants a vocabulary has learned.

`store.review_the_variants` asks again, one pair at a time, whether each
phrase learned as a variant of a relation is that relation, and with
`apply` separates the ones the model says are not ("toured" learned inside
`plans_to_visit` here; BUG-365's "visited" is in the static family map, so
its family would not be asked): the phrase is born again
under its own name and its facts move there, in every memory that shares the
vocabulary. Past phrases are left where they are (they sit in the relation of
their present form on purpose), and a closure between the two groups is
listed, not reopened. No model call: the answers are functions.
"""
# Port notes: on korely-agent's tables (the vocabulary per tenant,
# `PredicateCluster` / `PredicateAlias` with tenant_id, `AgentFact` with
# `facts.entities.TABLES`); the project's facts are the tenant's here
# (`AgentFact.tenant_id`), the scope `predicates.review_variants` passes.
# The product's command on top of it is tested in tests/test_review_vocabulary.py.
from __future__ import annotations

import json


from korely_agent.db.models import AgentFact, PredicateAlias, PredicateCluster
from korely_agent.services.facts import entities
from korely_agent.services.memory_core import store


def _vocabulary(break_family=False):
    asked = []

    def ask(prompt, think):
        asked.append(prompt)
        if "Known relations" in prompt:
            if 'New phrase: "toured"' in prompt:
                return json.dumps({"same_as": None})
            return json.dumps({"same_as": 1})
        if "many_values" in prompt:
            return json.dumps({"many_values": True})
        if break_family:
            raise RuntimeError("503 from the provider")
        return json.dumps({"family": "events"})

    ask.asked = asked
    return ask


def _fact(db, tenant, eu, raw, canonical, obj, tense="current"):
    f = AgentFact(tenant_id=tenant.id, end_user_id=eu, subject_label=eu, subject_type="person",
                  predicate_raw=raw, predicate_canonical=canonical, object_label=obj,
                  object_is_literal=False, confidence=0.9, tense=tense, predicate_family="plans")
    db.add(f)
    db.flush()
    return f


def _world(db, tenant):
    mem = entities.memory_of(tenant.id, None, "u1", None)
    rel = PredicateCluster(canonical="plans_to_visit", is_functional=False, family="plans", member_count=4,
                           example_object="Rome", **mem.new_relation)
    db.add(rel)
    db.flush()
    for s in ("plans_to_visit", "toured", "will_visit", "used_to_visit"):
        db.add(PredicateAlias(surface=s, cluster_id=rel.id, **mem.new_relation))
    rome = _fact(db, tenant, "u1", "plans to visit", "plans_to_visit", "Rome")
    paris = _fact(db, tenant, "u1", "toured", "plans_to_visit", "Paris")
    berlin = _fact(db, tenant, "u2", "toured", "plans_to_visit", "Berlin")
    _fact(db, tenant, "u1", "will visit", "plans_to_visit", "Oslo", tense="planned")
    _fact(db, tenant, "u1", "used to visit", "plans_to_visit", "Lyon", tense="past")
    rome.invalidated_by_id = paris.id      # the judge closed the plan with the visit
    db.commit()
    return mem, rel, rome, paris, berlin


def _project(tenant):
    return AgentFact.tenant_id == tenant.id


class TestWithoutApply:
    def test_says_what_it_would_separate_and_touches_nothing(self, db, tenant):
        mem, rel, _rome, paris, _b = _world(db, tenant)
        outcome = store.review_the_variants(db, entities.TABLES, mem, ask=_vocabulary(),
                                             facts=_project(tenant))
        assert outcome["separated"] == [("toured", "plans_to_visit", 2)]
        assert outcome["same"] == [("will_visit", "plans_to_visit")]
        assert outcome["to_the_past"] == [("used_to_visit", "plans_to_visit")]
        assert outcome["skipped"] == [] and outcome["closings_to_reread"] == []
        db.expire_all()
        assert db.get(AgentFact, paris.id).predicate_canonical == "plans_to_visit"
        assert db.query(PredicateAlias).filter_by(surface="toured").one().cluster_id == rel.id

    def test_the_question_carries_the_example_of_each(self, db, tenant):
        mem, *_ = _world(db, tenant)
        voc = _vocabulary()
        store.review_the_variants(db, entities.TABLES, mem, ask=voc, facts=_project(tenant))
        question = next(p for p in voc.asked if 'New phrase: "toured"' in p)
        assert "Rome" in question                       # the relation's example
        assert "Berlin" in question or "Paris" in question  # the phrase's own, from its latest fact
        # one option only: the relation the phrase sits in
        assert "1. " in question and "2. " not in question

    def test_the_past_is_not_asked(self, db, tenant):
        mem, *_ = _world(db, tenant)
        voc = _vocabulary()
        store.review_the_variants(db, entities.TABLES, mem, ask=voc, facts=_project(tenant))
        assert not any('"used to visit"' in p for p in voc.asked)


class TestWithApply:
    def test_the_variant_is_born_alone_and_the_facts_follow_it(self, db, tenant):
        mem, rel, rome, paris, berlin = _world(db, tenant)
        outcome = store.review_the_variants(db, entities.TABLES, mem, ask=_vocabulary(), apply=True,
                                             facts=_project(tenant))
        db.commit()
        assert outcome["separated"] == [("toured", "plans_to_visit", 2)]
        new = db.query(PredicateCluster).filter_by(canonical="toured").one()
        assert new.family == "events"
        assert db.query(PredicateAlias).filter_by(surface="toured").one().cluster_id == new.id
        # both end users of the project, as the vocabulary is the project's
        for f in (paris, berlin):
            row = db.get(AgentFact, f.id)
            assert (row.predicate_canonical, row.predicate_family) == ("toured", "events")
        assert db.get(AgentFact, rome.id).predicate_canonical == "plans_to_visit"
        assert db.get(PredicateCluster, rel.id).member_count == 3
        # the plan closed by the visit is listed for a reader, not reopened
        assert outcome["closings_to_reread"] == [(rome.id, paris.id)]
        assert db.get(AgentFact, rome.id).invalidated_by_id == paris.id

    def test_a_birth_that_fails_leaves_everything_as_it_was(self, db, tenant):
        mem, rel, _rome, paris, _b = _world(db, tenant)
        outcome = store.review_the_variants(db, entities.TABLES, mem, ask=_vocabulary(break_family=True),
                                             apply=True, facts=_project(tenant))
        db.commit()
        assert outcome["skipped"] == [("toured", "plans_to_visit")] and outcome["separated"] == []
        assert db.query(PredicateAlias).filter_by(surface="toured").one().cluster_id == rel.id
        assert db.get(AgentFact, paris.id).predicate_canonical == "plans_to_visit"
        assert db.query(PredicateCluster).filter_by(canonical="toured").count() == 0
        assert db.get(PredicateCluster, rel.id).member_count == 4

    def test_without_the_project_facts_it_stays_in_the_memory(self, db, tenant):
        # Without `facts`, only the memory's own facts move (u1's Paris, not u2's Berlin).
        mem, _rel, _rome, paris, berlin = _world(db, tenant)
        outcome = store.review_the_variants(db, entities.TABLES, mem, ask=_vocabulary(), apply=True)
        db.commit()
        assert outcome["separated"] == [("toured", "plans_to_visit", 1)]
        assert db.get(AgentFact, paris.id).predicate_canonical == "toured"
        assert db.get(AgentFact, berlin.id).predicate_canonical == "plans_to_visit"


def _in_turn(*answers):
    """The same-relation answers for "toured", one per call; the other phrases are the same relation."""
    queue = list(answers)
    asked = []

    def ask(prompt, think):
        if "Known relations" in prompt:
            if 'New phrase: "toured"' in prompt:
                asked.append(think)
                return json.dumps({"same_as": queue.pop(0)})
            return json.dumps({"same_as": 1})
        return json.dumps({"many_values": True}) if "many_values" in prompt else json.dumps({"family": "events"})

    ask.asked = asked
    return ask


class TestTheVotes:
    """8 Oct: a separation is kept for good, so "different" needs a majority;
    the first "same" ends the asking."""

    def _outcome(self, db, tenant, *answers):
        mem, *_ = _world(db, tenant)
        voc = _in_turn(*answers)
        outcome = store.review_the_variants(db, entities.TABLES, mem, ask=voc, facts=_project(tenant), votes=3)
        return [s for s, *_ in outcome["separated"]], voc.asked

    def test_the_first_same_stops(self, db, tenant):
        assert self._outcome(db, tenant, 1) == ([], [False])

    def test_two_different_separate(self, db, tenant):
        assert self._outcome(db, tenant, None, None) == (["toured"], [False, True])

    def test_different_same_same_stays(self, db, tenant):
        assert self._outcome(db, tenant, None, 1, 1) == ([], [False, True, True])

    def test_different_same_different_separates(self, db, tenant):
        assert self._outcome(db, tenant, None, 1, None) == (["toured"], [False, True, True])

    def test_one_vote_as_before(self, db, tenant):
        mem, *_ = _world(db, tenant)
        outcome = store.review_the_variants(db, entities.TABLES, mem, ask=_in_turn(None),
                                             facts=_project(tenant))
        assert [s for s, *_ in outcome["separated"]] == ["toured"]
