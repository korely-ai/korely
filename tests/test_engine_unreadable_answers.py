"""memory_core 2.2.0: an unreadable answer is asked again, it does not make the write fail.

In the LongMemEval measurement of 29 Sep (development, synchronous writes) 3
memories out of ~450 ended up with no facts because the vocabulary had given
an answer that the strict reader discards: twice the family "interests" (a
word from the description of "preferences", not an offered name), once
'Formatting:** * `{"many_values": false}`{"many_values": false}' (the right
answer, repeated, with text around it). With three votes, one
unreadable answer out of three was enough. Now: the reader reads a repeated
object if all the copies say the same thing, and an unreadable answer is asked
again, thinking, up to RETRIES_UNREADABLE times, before raising as before.

No model is called: the answers are queued.
"""
# Port notes: the new relation is born in korely-agent's vocabulary
# (`PredicateCluster` per tenant, `facts.entities.memory_of`).
from __future__ import annotations

import json

import pytest

from korely_agent.db.models import PredicateCluster
from korely_agent.services.facts import entities
from korely_agent.services.memory_core import store, vocabulary as voc

THE_REAL_ANSWER = 'Formatting:** * `{"many_values": false}`{"many_values": false}'


def _queued(*answers):
    queue = list(answers)
    asked = []

    def ask(prompt, think):
        asked.append(think)
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps({"many_values": r})

    ask.think = asked
    return ask


MANY, ONE = True, False   # the value of "many_values"


class TestTheReader:
    def test_the_repeated_answer_with_text_around_it_is_read(self):
        assert voc.read_single_value(THE_REAL_ANSWER, severe=True) is True

    def test_two_different_answers_in_the_same_text_are_not_read(self):
        with pytest.raises(voc.UnreadableAnswer):
            voc.read_single_value('{"many_values": false} or maybe {"many_values": true}', severe=True)

    def test_a_single_object_with_text_around_it_is_read_as_before(self):
        assert voc.read_family('I think: {"family": "financial"} is right', severe=True) == "financial"

    def test_a_truncated_object_stays_unreadable(self):
        with pytest.raises(voc.UnreadableAnswer):
            voc.read_same_relation('{"same_as": 1', 3, severe=True)


class TestItIsAskedAgain:
    def test_after_an_unreadable_answer_it_is_asked_again_thinking(self):
        c = _queued('{"family": "interests"}', '{"family": "preferences"}')
        fam = voc.ask_and_read(c, "which family?", False, voc.read_family, severe=True)
        assert fam == "preferences"
        assert c.think == [False, True]

    def test_after_the_retries_it_raises_as_before(self):
        c = _queued(*(['{"family": "interests"}'] * (voc.RETRIES_UNREADABLE + 1)))
        with pytest.raises(voc.UnreadableAnswer):
            voc.ask_and_read(c, "which family?", False, voc.read_family, severe=True)
        assert len(c.think) == voc.RETRIES_UNREADABLE + 1

    def test_without_severity_it_is_a_single_question_as_before(self):
        c = _queued('{"family": "interests"}')
        assert voc.ask_and_read(c, "which family?", False, voc.read_family) is None
        assert c.think == [False]

    def test_a_failed_call_is_not_asked_again_here(self):
        """A failed call is the engine's to retry, not this loop's."""
        c = _queued(RuntimeError("quota"), '{"family": "financial"}')
        with pytest.raises(RuntimeError):
            voc.ask_and_read(c, "which family?", False, voc.read_family, severe=True)
        assert len(c.think) == 1


class TestTheVotes:
    def test_an_unreadable_vote_is_asked_again_and_the_majority_decides(self):
        c = _queued(ONE, "garbage", ONE)
        assert voc.decide_single_value(c, "subscribes_to", "Basic plan", severe=True) is True
        assert len(c.think) == 3

    def test_the_real_answer_of_29_sep_is_a_vote(self):
        c = _queued(THE_REAL_ANSWER, ONE)
        assert voc.decide_single_value(c, "located_at", "Berlin", severe=True) is True
        assert len(c.think) == 2


def _memory(tenant):
    return entities.memory_of(tenant.id, None, "u1", None)


class TestTheNewRelation:
    def test_the_family_interests_is_asked_again_and_the_relation_is_born(self, db, tenant):
        """The real case of 29 Sep: 'builds' had no facts."""
        mem = _memory(tenant)
        c = _queued(MANY, MANY, '{"family": "interests"}', '{"family": "preferences"}')
        rel = store.resolve_predicate(
            db, entities.TABLES, mem, "builds", "a birdhouse",
            ask=c, embed=None, strict=True, severe=True, single_value_votes=voc.SINGLE_VALUE_VOTES,
        )
        assert rel.resolved and rel.family == "preferences" and rel.is_functional is False
        row = db.query(PredicateCluster).filter(PredicateCluster.canonical == "builds").one()
        assert row.family == "preferences"

    def test_if_it_stays_unreadable_nothing_is_saved(self, db, tenant):
        mem = _memory(tenant)
        c = _queued(MANY, MANY, *(['{"family": "interests"}'] * (voc.RETRIES_UNREADABLE + 1)))
        with pytest.raises(store.ResolutionUnusable):
            store.resolve_predicate(
                db, entities.TABLES, mem, "builds", "a birdhouse",
                ask=c, embed=None, strict=True, severe=True,
                single_value_votes=voc.SINGLE_VALUE_VOTES,
            )
        assert db.query(PredicateCluster).filter(PredicateCluster.canonical == "builds").count() == 0
