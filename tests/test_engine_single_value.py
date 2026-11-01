"""A plan change did not close the old plan (28 Sep 2026).

The first time a memory meets a relation, the vocabulary asks whether a
subject has only one value of it at a time, and keeps the answer for ever.
For `subscribes_to ... plan` the question said "many" 3 times in 10: from that
moment the judge did not receive [single-valued] and the two plans stayed open
(4 new installations out of 21 in korely-agent). Now the question is typed
(Lin, Mausam, Etzioni, EMNLP 2010 §3 "Type Restrictions": measured, 0 errors
in 115), the decision is by majority
(self-consistency, Wang et al., ICLR 2023), and the memories already poisoned
are reclassified.

No model is called: the answers are queued.
"""
# Notes:
#   * The comparison of the engine's question with the measured one, byte for
#     byte, stays with the measurement script, which this repository does not
#     have.
#   * The DB tests run on korely-agent's vocabulary (`PredicateCluster` per
#     tenant, `facts.entities.memory_of`).
from __future__ import annotations

import json

import pytest

from korely_agent.db.models import PredicateCluster
from korely_agent.services.facts import entities
from korely_agent.services.memory_core import store, vocabulary as voc


def _queued(*answers):
    queue = list(answers)
    asked = []

    def ask(prompt, think):
        asked.append(prompt)
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps({"many_values": r})

    ask.asked = asked
    return ask


MANY, ONE = True, False   # the value of "many_values"


class TestTheQuestion:
    def test_typed_when_there_is_the_example(self):
        d = voc.single_value_question("subscribes_to", "Priority Support plan")
        assert 'of the same kind as "Priority Support plan"' in d
        assert 'for the relation "subscribes to"' in d and "published in" in d
        # 2.5.0: asked as replacement, the wording measured on 30 Sep; 3.2.0
        # adds one many-valued example, the question stays the same.
        assert "USUALLY REPLACE the old one" in d and "hold together" in d
        assert "MORE THAN ONE value" not in d
        # What the fakes recognise the question by, here and in korely-agent.
        assert '"many_values"' in d

    def test_without_the_example_the_general_question_stays(self):
        d = voc.single_value_question("subscribes_to")
        assert 'Relation: "subscribes to"' in d and "same kind" not in d


class TestTheMajority:
    def test_two_equal_answers_are_enough(self):
        c = _queued(ONE, ONE)
        assert voc.decide_single_value(c, "subscribes_to", "Basic plan") is True
        assert len(c.asked) == 2

    def test_a_split_asks_for_the_third(self):
        c = _queued(MANY, ONE, ONE)
        assert voc.decide_single_value(c, "subscribes_to", "Basic plan") is True
        assert len(c.asked) == 3

    def test_without_a_majority_it_stays_undecided(self):
        c = _queued(MANY, ONE, '{"x": 1}')
        assert voc.decide_single_value(c, "subscribes_to", "Basic plan", severe=False) is None

    def test_a_failed_call_is_not_a_vote(self):
        with pytest.raises(RuntimeError):
            voc.decide_single_value(_queued(ONE, RuntimeError("quota")), "subscribes_to", "Basic plan")

    def test_with_a_single_vote_it_is_as_before(self):
        c = _queued(MANY)
        assert voc.decide_single_value(c, "subscribes_to", "Basic plan", votes=1) is False
        assert len(c.asked) == 1


def _memory(tenant):
    return entities.memory_of(tenant.id, None, "u1", None)


def test_before_the_first_answer_many_no_longer_decides_alone(db, tenant):
    """(before) a single answer, "many", kept `subscribes_to` many-valued for ever."""
    mem = _memory(tenant)
    c = _queued(MANY, ONE, ONE, '{"family": "financial"}')
    rel = store.resolve_predicate(
        db, entities.TABLES, mem, "subscribes_to", "Priority Support plan",
        ask=c, embed=None, strict=True, severe=True, single_value_votes=voc.SINGLE_VALUE_VOTES,
    )
    assert rel.is_functional is True
    row = db.query(PredicateCluster).filter(PredicateCluster.canonical == "subscribes_to").one()
    assert row.is_functional is True and row.example_object == "Priority Support plan"


class TestTheReclassification:
    def _poisoned(self, db, tenant):
        mem = _memory(tenant)
        db.add(PredicateCluster(canonical="subscribes_to", is_functional=False, family="financial",
                                member_count=3, example_object="Priority Support plan",
                                **mem.new_relation))
        db.add(PredicateCluster(canonical="likes", is_functional=False, family="preferences",
                                member_count=2, example_object="fish", **mem.new_relation))
        db.commit()
        return mem

    def test_without_apply_it_says_what_would_change_and_touches_nothing(self, db, tenant):
        mem = self._poisoned(db, tenant)
        outcome = store.reclassify_single_value(db, entities.TABLES, mem, ask=_queued(MANY, MANY, ONE, ONE))
        assert outcome["changed"] == [("subscribes_to", False, True)] and outcome["same"] == ["likes"]
        db.expire_all()
        assert db.query(PredicateCluster).filter_by(canonical="subscribes_to").one().is_functional is False

    def test_with_apply_the_memory_heals(self, db, tenant):
        mem = self._poisoned(db, tenant)
        store.reclassify_single_value(db, entities.TABLES, mem, ask=_queued(MANY, MANY, ONE, ONE),
                                             apply=True)
        db.commit()
        assert db.query(PredicateCluster).filter_by(canonical="subscribes_to").one().is_functional is True
        assert db.query(PredicateCluster).filter_by(canonical="likes").one().is_functional is False

    def test_a_relation_that_does_not_answer_stays_as_it_is(self, db, tenant):
        mem = self._poisoned(db, tenant)
        outcome = store.reclassify_single_value(
            db, entities.TABLES, mem, ask=_queued(RuntimeError("quota"), ONE, ONE), apply=True)
        assert outcome["skipped"] == ["likes"] and outcome["changed"] == [("subscribes_to", False, True)]

    def test_a_relation_without_an_example_is_not_asked_again(self, db, tenant):
        """Without the value it was seen with, only the general question is left,
        the one that said "many" for the plans: asking it again would bring it
        back (found the night of 28 Sep, `subscribes_to` of the Agents)."""
        mem = _memory(tenant)
        db.add(PredicateCluster(canonical="subscribes_to", is_functional=True, family="financial",
                                member_count=1, example_object=None, **mem.new_relation))
        db.commit()
        c = _queued()
        outcome = store.reclassify_single_value(db, entities.TABLES, mem, ask=c, apply=True)
        assert outcome["same"] == ["subscribes_to"] and c.asked == []
