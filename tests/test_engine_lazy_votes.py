"""memory_core 3.3.0, work item 8: the single-value votes
asked when a judge needs them.

At birth (`resolve_predicate(lazy_votes=True)`) a relation keeps its
cardinality undecided; the votes are asked, with the same question and the
same example, when a new fact meets an earlier fact of the SAME relation
(`store.decide_if_missing` through the judge's `ensure`). Undecided reads
as today: no tag. Off, nothing changes.

No model call: the vocabulary and the judge are functions.
"""
# Port notes: on korely-agent's vocabulary (`PredicateCluster` per tenant,
# `facts.entities.memory_of`; the helpers of tests/test_engine_uncovered_branches.py).
# TestInTheCloud ran on the hosted adapter; here on this product's:
#   * test_an_unreadable_vote_after_the_vector_does_not_break_the_job: the hosted
#     `judge_after_the_vector` caught its unreadable-vocabulary error;
#     korely-agent's `facts.contradiction.judge_after_the_vector` catches the
#     engine's ResolutionUnusable (with JudgeUnreadable) and returns {}.
#   * test_off_no_ensure is already here:
#     tests/test_memory_core_3_3_adapter.py::TestTheLazyVotes::
#     test_off_the_judges_get_nothing_to_ask (`predicates.LAZY_SINGLE_VALUE`
#     off: `contradiction._ensure` is None; on here since 0.1.23, which that
#     test asserts, where the hosted default was off). Not duplicated.
#   * test_on_the_birth_is_lazy: the switch is `predicates.LAZY_SINGLE_VALUE`
#     and the birth `predicates.resolve_relation`.
#   * test_on_ensure_decides_with_the_projects_vocabulary: through
#     `contradiction._ensure(db, tenant)` and the real `store.decide_if_missing`,
#     the vocabulary's model faked at `predicates._ask`.
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import PredicateCluster
from korely_agent.services import predicates
from korely_agent.services.facts import contradiction as contr
from korely_agent.services.facts import entities
from korely_agent.services.memory_core import store, judge
from tests.test_engine_uncovered_branches import _Vocabulary


def _mem(tenant):
    return entities.memory_of(tenant.id, None, "u1", None)


def _resolve(db, tenant, phrase, voc, **kw):
    return store.resolve_predicate(db, entities.TABLES, _mem(tenant), phrase, "Business plan", ask=voc,
                                      embed=None, strict=True, severe=True, single_value_votes=3, **kw)


def _row(db, tenant, canonical):
    return db.query(PredicateCluster).filter(PredicateCluster.tenant_id == tenant.id,
                                             PredicateCluster.canonical == canonical).one()


class TestAtBirth:
    def test_by_default_the_vote_is_at_birth(self, db, tenant):
        voc = _Vocabulary(many=False)
        rel = _resolve(db, tenant, "on_plan", voc)
        assert "value" in voc.asked and rel.is_functional is True

    def test_lazy_no_vote_and_undecided(self, db, tenant):
        voc = _Vocabulary(many=False)
        rel = _resolve(db, tenant, "on_plan", voc, lazy_votes=True)
        assert "value" not in voc.asked
        assert "family" in voc.asked and rel.family == "preferences"  # the family is asked as before
        assert rel.is_functional is None and _row(db, tenant, "on_plan").is_functional is None
        # Undecided reads as today: no tag.
        assert store.single_value(db, entities.TABLES, _mem(tenant), "on_plan") == (False, None)


class TestDecideIfMissing:
    def test_votes_once_with_the_example_of_the_birth_and_keeps_it(self, db, tenant):
        _resolve(db, tenant, "on_plan", _Vocabulary(), lazy_votes=True)
        voc = _Vocabulary(many=False)
        tag = store.decide_if_missing(db, entities.TABLES, _mem(tenant), "on_plan", voc, severe=True, votes=3)
        assert tag == (True, "Business plan")
        assert voc.asked == ["value", "value"]  # two agreeing votes end the vote
        assert _row(db, tenant, "on_plan").is_functional is True
        voc2 = _Vocabulary(many=True)
        assert store.decide_if_missing(db, entities.TABLES, _mem(tenant), "on_plan", voc2, severe=True,
                                        votes=3) == (True, "Business plan")
        assert voc2.asked == []  # decided for good

    def test_an_already_decided_relation_is_not_asked_again(self, db, tenant):
        _resolve(db, tenant, "likes", _Vocabulary(many=True))
        voc = _Vocabulary(many=False)
        assert store.decide_if_missing(db, entities.TABLES, _mem(tenant), "likes", voc, votes=3) == (False, None)
        assert voc.asked == []

    def test_a_relation_without_an_example_stays_undecided(self, db, tenant):
        """An old relation without its example keeps its tag undecided, as
        `reclassify_single_value` leaves it (the review of 7 Oct)."""
        r = PredicateCluster(tenant_id=tenant.id, canonical="mentors", centroid=None, is_functional=None,
                             family="people", member_count=1, example_object=None)
        db.add(r)
        db.commit()
        voc = _Vocabulary(many=False)
        assert store.decide_if_missing(db, entities.TABLES, _mem(tenant), "mentors", voc, votes=3) == (False, None)
        assert voc.asked == []

    def test_without_a_model_nothing(self, db, tenant):
        _resolve(db, tenant, "on_plan", _Vocabulary(), lazy_votes=True)
        assert store.decide_if_missing(db, entities.TABLES, _mem(tenant), "on_plan", None) == (False, None)
        assert _row(db, tenant, "on_plan").is_functional is None

    def test_a_model_that_does_not_answer_saves_nothing(self, db, tenant):
        _resolve(db, tenant, "on_plan", _Vocabulary(), lazy_votes=True)
        with pytest.raises(store.ResolutionUnavailable):
            store.decide_if_missing(db, entities.TABLES, _mem(tenant), "on_plan",
                                     _Vocabulary(fail={"value"}), severe=True, votes=3)
        assert _row(db, tenant, "on_plan").is_functional is None

    def test_an_unusable_answer_is_unusable(self, db, tenant):
        _resolve(db, tenant, "on_plan", _Vocabulary(), lazy_votes=True)
        with pytest.raises(store.ResolutionUnusable):
            store.decide_if_missing(db, entities.TABLES, _mem(tenant), "on_plan",
                                     _Vocabulary(fail={"value_unreadable"}), severe=True, votes=3)


def _candidate(predicate, obj):
    return NS(id=uuid.uuid4(), subject_label="Acme", subject_canonical="acme", predicate_raw=predicate,
              predicate_canonical=predicate, object_label=obj,
              valid_from=datetime(2026, 1, 5, tzinfo=timezone.utc))


class _Judge:
    def __init__(self):
        self.prompt = []

    def __call__(self, prompt, n):
        self.prompt.append(prompt)
        ids = [r.split("]")[0] for r in prompt.split("[new_id=")[1:]]
        if ids:
            return json.dumps({"results": [{"new_id": i, "contradictions": []} for i in ids]})
        return json.dumps({"contradictions": []})


class TestTheJudgeAsksOnlyBeforeTheSameRelation:
    def test_one_fact_at_a_time(self):
        asked = []

        def ensure(rel, examples=()):
            asked.append(rel)
            values.append(list(examples))
            return True, "Business plan"

        values = []

        g = _Judge()
        judge.judge(g, [_candidate("on_plan", "Business plan")], "Acme", "on_plan", "Starter plan",
                        single_value=lambda c: (False, None), relation="on_plan", ensure=ensure)
        assert asked == ["on_plan"]
        # 8 Oct: typed by the two values in conflict, the new one and the earlier one.
        assert values == [["Starter plan", "Business plan"]]
        assert "Acme on_plan Starter plan [single-valued for values like: Business plan]" in g.prompt[0]
        asked.clear()
        judge.judge(g, [_candidate("likes", "jazz")], "Acme", "on_plan", "Starter plan",
                        single_value=lambda c: (False, None), relation="on_plan", ensure=ensure)
        assert asked == []

    def test_a_past_fact_does_not_ask_for_the_votes(self):
        """Judged without the tag, as before 3.3.0 (the review of 7 Oct)."""
        asked = []
        g = _Judge()
        judge.judge(g, [_candidate("on_plan", "Business plan")], "Acme", "on_plan", "Starter plan",
                        single_value=lambda c: (False, None), past=True, relation="on_plan",
                        ensure=lambda rel: asked.append(rel) or (True, "Business plan"))
        assert asked == [] and "[single-valued" not in g.prompt[0].split("NEW fact:")[1]

    def test_the_log_names_the_canonical_relation(self, caplog):
        import logging

        g = _Judge()
        with caplog.at_level(logging.INFO, logger=judge.__name__):
            judge.judge(g, [_candidate("on_plan", "Business plan")], "Acme", "is on the plan", "Starter plan",
                            single_value=lambda c: (False, None), relation="on_plan")
        [row] = [r.getMessage() for r in caplog.records if "shows" in r.getMessage()]
        assert row.endswith("shows 1 relations: on_plan")

    def test_in_batches(self, db):
        asked = []
        new_facts = [dict(id=uuid.uuid4(), subject="Acme", predicate_raw="on_plan", predicate_canonical="on_plan",
                      object="Starter plan", valid_from=None),
                 dict(id=uuid.uuid4(), subject="Acme", predicate_raw="uses", predicate_canonical="uses",
                      object="Slack", valid_from=None)]
        plan, taste = _candidate("on_plan", "Business plan"), _candidate("likes", "jazz")
        judge.batch(db, None, new_facts,
                      gather=lambda nf: [plan] if nf["predicate_canonical"] == "on_plan" else [taste],
                      single_value_of_new=lambda nf: (False, None),
                      classify=lambda groups: {},
                      ensure=lambda rel, examples=(): asked.append((rel, list(examples))) or (True, None))
        assert asked == [("on_plan", ["Starter plan", "Business plan"])]


class TestInTheCloud:
    def test_an_unreadable_vote_after_the_vector_does_not_break_the_job(self, db, tenant, monkeypatch):
        def _broken(*a, **k):
            raise store.ResolutionUnusable("unusable answer on the cardinality of 'on_plan'")

        monkeypatch.setattr(judge, "after_the_vector", _broken)
        fact = NS(id=uuid.uuid4(), project_id=None, end_user_id="u1")
        assert contr.judge_after_the_vector(db, tenant.id, fact, [0.1] * EMBEDDING_DIM) == {}

    def test_on_the_birth_is_lazy(self, db, tenant, monkeypatch):
        seen = {}
        monkeypatch.setattr(predicates, "LAZY_SINGLE_VALUE", True)
        monkeypatch.setattr(store, "resolve_predicate",
                            lambda *a, **k: seen.update(k) or store.Relation("on_plan", None, "other"))
        predicates.resolve_relation(db, tenant.id, "on_plan", object_label="Business plan",
                                    end_user_id=None)
        assert seen["lazy_votes"] is True

    def test_on_ensure_decides_with_the_projects_vocabulary(self, db, tenant, monkeypatch):
        _resolve(db, tenant, "on_plan", _Vocabulary(), lazy_votes=True)
        monkeypatch.setattr(predicates, "LAZY_SINGLE_VALUE", True)
        voc = _Vocabulary(many=False)
        monkeypatch.setattr(predicates, "_ask", lambda: voc)
        ensure = contr._ensure(db, tenant.id, None, "u1")
        assert ensure("on_plan") == (True, "Business plan") and voc.asked == ["value", "value"]


class TestTheTwoValuesInConflict:
    """8 Oct, the confirmation run of 3.3.0: `reads` learned from "reads
    reading" was voted one value typed by "reading". The vote at the first
    meeting is typed by the two values in conflict, and holds one value only
    when it does for every one of them."""

    def _born(self, db, tenant, example="reading"):
        db.add(PredicateCluster(canonical="reads", is_functional=None, family="preferences", member_count=1,
                                example_object=example, **_mem(tenant).new_relation))
        db.commit()

    def _votes(self, answers):
        queue = dict(answers)
        asked = []

        def ask(prompt, think):
            for value, many in queue.items():
                if f'"{value}"' in prompt:
                    asked.append(value)
                    return json.dumps({"many_values": many})
            raise AssertionError("unexpected question")
        ask.asked = asked
        return ask

    def test_one_of_the_two_holds_many_and_that_is_enough(self, db, tenant):
        self._born(db, tenant)
        c = self._votes({"Dune": True, "a fantasy series": False})
        assert store.decide_if_missing(db, entities.TABLES, _mem(tenant), "reads", c,
                                        examples=["Dune", "a fantasy series"]) == (False, None)
        assert set(c.asked) == {"Dune", "a fantasy series"} and "reading" not in c.asked
        assert _row(db, tenant, "reads").is_functional is False

    def test_single_only_when_it_is_for_all(self, db, tenant):
        self._born(db, tenant)
        c = self._votes({"Business plan": False, "Starter plan": False})
        assert store.decide_if_missing(db, entities.TABLES, _mem(tenant), "reads", c,
                                        examples=["Starter plan", "Business plan"])[0] is True

    def test_without_examples_the_birth_one(self, db, tenant):
        self._born(db, tenant)
        c = self._votes({"reading": False})
        assert store.decide_if_missing(db, entities.TABLES, _mem(tenant), "reads", c)[0] is True
        assert c.asked == ["reading"]

    def test_the_same_value_once(self, db, tenant):
        self._born(db, tenant)
        c = self._votes({"Dune": True})
        store.decide_if_missing(db, entities.TABLES, _mem(tenant), "reads", c, examples=["Dune", " Dune "])
        assert c.asked == ["Dune"]
