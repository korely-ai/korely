"""memory_core 2.12.0: the branches no test executed.

The coverage measured by the Self-hosted on its copy (an audit on 2.10.0:
`resolve_predicate` 72%, `write_fact` 76%,
`judge_batch`, `after_the_vector` and `find_twin` almost never executed) and,
measured here on 6 October with a standard-library tracer (pytest-cov
is not installed in the test container): `after_the_vector` 1 branch in 20,
`resolve_predicate` 24 in 42, `judge_batch` 12 in 16. `relative_time.read`
is already fully covered here (42 branches in 42, test_engine_relative_time).

No model is called.
"""
# Notes: on korely-agent's tables (tenant_id; the vocabulary per tenant,
# `PredicateCluster` / `PredicateAlias` with tenant_id; `facts.entities.TABLES`,
# `memory_scope`) and on PostgreSQL:
#   * TestAfterTheVector ran on SQLite with the PostgreSQL gates forced open
#     (`store._pg`, `candidates._pg`); here the gates are open for real and
#     the nearest rows come from pgvector, so that fixture is gone.
#   * test_a_failing_vector_off_postgres_decides_nothing tests
#     the branch off PostgreSQL: here it is reached by closing the gate
#     (`store._pg` -> False), the test's own subject.
#   * test_a_variant_that_points_to_a_relation_that_is_gone: here
#     `predicate_aliases.cluster_id` is a foreign key (ON DELETE CASCADE), so a
#     variant whose relation is gone exists only for a reader racing a delete;
#     the test builds that state with the foreign keys off for its one insert
#     (`session_replication_role = replica`, a superuser's setting, as the test
#     database's user is).
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory, PredicateAlias, PredicateCluster
from korely_agent.services.facts import entities
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import store, twins, judge, thresholds
from korely_agent.services.memory_core.store import Relation
from korely_agent.services.memory_core.writing import write_fact


def _d(*a):
    return datetime(*a, tzinfo=timezone.utc)


def _aware(v):
    return None if v is None else (v if v.tzinfo else v.replace(tzinfo=timezone.utc))


# ----------------------------------------------------------- judge_batch

class TestJudgeBatch:
    def _c(self):
        return NS(id=uuid.uuid4(), subject_label="Maria", subject_canonical="Maria", predicate_raw="works_at",
                  object_label="Acme", valid_from=None, predicate_canonical="works_at", project_id=None)

    def test_without_groups_no_call(self):
        assert judge.judge_batch(lambda p, n: pytest.fail("called"), [], single_value=lambda c: (False, None)) == {}

    def test_crooked_entries_are_skipped_and_count_as_missing(self):
        a, b = self._c(), self._c()
        groups = [{"new_id": "n1", "new_text": "x", "candidates": [a]},
                  {"new_id": "n2", "new_text": "y", "candidates": [b]}]
        answer = json.dumps({"results": [
            "not an object",
            {"new_id": "invented", "contradictions": [{"existing_id": str(a.id)}]},
            {"new_id": "n1", "contradictions": "not a list"},
            {"new_id": "n2", "contradictions": [{"existing_id": str(b.id)}]},
        ]})
        # n1 has no list: not judged, so no verdict.
        with pytest.raises(judge.JudgementUnreadable):
            judge.judge_batch(lambda p, n: answer, groups, single_value=lambda c: (False, None))

    def test_a_group_without_contradictions_does_not_enter_the_result(self):
        a = self._c()
        groups = [{"new_id": "n1", "new_text": "x", "candidates": [a]}]
        out = judge.judge_batch(lambda p, n: json.dumps({"results": [{"new_id": "n1", "contradictions": []}]}),
                                    groups, single_value=lambda c: (False, None))
        assert out == {}


# ------------------------------------------------------------ find_twin

def test_find_twin_a_past_phrase_has_its_own_way(db, tenant):
    f = NS(id=uuid.uuid4(), embedding=[0.1] * EMBEDDING_DIM, tense="past")
    assert twins.find_twin(db, entities.TABLES, entities.memory_of(tenant.id, None, "u1", None), f,
                                 family="preferences", is_open=true_at(), thresholds=thresholds.Twin(0.1, 0.05)) is None


# ----------------------------------------------------------- after_the_vector

V = [1.0] + [0.0] * (EMBEDDING_DIM - 1)


class TestAfterTheVector:
    """On SQLite with the PostgreSQL gates open: `vector_search.nearest` computes
    the distances in Python, and the rest runs as in production."""

    def _fact(self, db, tenant, subject, predicate, obj, *, since, created, vector=V, tense="current"):
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1", subject_label=subject,
                      subject_canonical=subject, subject_type="person", predicate_raw=predicate,
                      predicate_canonical=predicate, object_label=obj, object_canonical=obj,
                      valid_from=since, created_at=created, embedding=vector, tense=tense)
        db.add(f)
        db.commit()
        return f

    def _judge(self, db, tenant, fact, verdicts):
        seen = []

        def classify(groups):
            seen.extend(groups)
            return verdicts(groups)

        out = judge.after_the_vector(db, entities.TABLES, entities.memory_scope(tenant.id, None, "u1"), fact,
                                      V, is_open=true_at(), single_value=lambda c: (False, None),
                                      classify=classify)
        return out, seen

    def test_an_earlier_fact_that_only_the_meaning_finds(self, db, tenant):
        old = self._fact(db, tenant, "the runner", "best_time", "27:12",
                         since=_d(2025, 1, 1), created=_d(2025, 1, 1))
        late = self._fact(db, tenant, "Maria", "personal_best", "25:50",
                          since=_d(2025, 6, 1), created=_d(2025, 6, 1), vector=None)
        out, seen = self._judge(db, tenant, late, lambda g: {str(late.id): [old.id]})
        assert [g["new_id"] for g in seen] == [str(late.id)]
        assert "(since 2025-06-01)" in seen[0]["new_text"]
        assert out == {late.id: [old.id]}
        assert _aware(old.invalid_at) == _d(2025, 6, 1)

    def test_an_earlier_fact_started_later_closes_the_late_fact(self, db, tenant):
        """Created earlier but started later (an import): the late fact is history."""
        old = self._fact(db, tenant, "the runner", "best_time", "27:12",
                         since=_d(2025, 9, 1), created=_d(2025, 1, 1))
        late = self._fact(db, tenant, "Maria", "personal_best", "25:50",
                          since=_d(2025, 6, 1), created=_d(2025, 6, 1), vector=None)
        out, _ = self._judge(db, tenant, late, lambda g: {str(late.id): [old.id]})
        assert out == {old.id: [late.id]}
        assert old.invalid_at is None and _aware(late.invalid_at) == _d(2025, 9, 1)

    def test_the_later_facts_in_the_order_in_which_they_start(self, db, tenant):
        """Two facts written later, near by meaning: the one that started
        before the late fact is its history, the one that started after closes it."""
        late = self._fact(db, tenant, "Maria", "personal_best", "25:50",
                          since=_d(2025, 6, 1), created=_d(2025, 6, 1), vector=None)
        before = self._fact(db, tenant, "the runner", "best_time", "27:12",
                            since=_d(2025, 3, 1), created=_d(2025, 7, 1))
        after = self._fact(db, tenant, "the athlete", "record_time", "24:59",
                           since=_d(2025, 9, 1), created=_d(2025, 8, 1))
        out, seen = self._judge(db, tenant, late,
                                lambda g: {x["new_id"]: [late.id] for x in g})
        assert {g["new_id"] for g in seen} == {str(before.id), str(after.id)}
        assert _aware(before.invalid_at) == _d(2025, 6, 1) and before.invalidated_by_id == late.id
        assert _aware(late.invalid_at) == _d(2025, 9, 1) and late.invalidated_by_id == after.id
        assert after.invalid_at is None
        assert out == {late.id: [before.id], after.id: [late.id]}

    def test_nothing_to_do(self, db, tenant):
        late = self._fact(db, tenant, "Maria", "personal_best", "25:50",
                          since=_d(2025, 6, 1), created=_d(2025, 6, 1), vector=None)
        def never(g):
            pytest.fail("no group, no call")
        assert self._judge(db, tenant, late, never)[0] == {}
        scope = entities.memory_scope(tenant.id, None, "u1")
        assert judge.after_the_vector(db, entities.TABLES, scope, late, None, is_open=true_at(),
                                       single_value=lambda c: (False, None), classify=never) == {}
        past = self._fact(db, tenant, "Maria", "lived_in", "Rome", since=_d(2020, 1, 1),
                              created=_d(2025, 6, 2), vector=None, tense="past")
        assert judge.after_the_vector(db, entities.TABLES, scope, past, V, is_open=true_at(),
                                       single_value=lambda c: (False, None), classify=never) == {}
        late.invalid_at = _d(2025, 7, 1)
        db.commit()
        assert judge.after_the_vector(db, entities.TABLES, scope, late, V, is_open=true_at(),
                                       single_value=lambda c: (False, None), classify=never) == {}


# ------------------------------------------------------------- write_fact

LIKES = Relation(canonical="likes", is_functional=False, family="preferences")


def _memory(db, tenant):
    m = AgentMemory(tenant_id=tenant.id, end_user_id="u1", content="x", graph_sync_status="done")
    db.add(m)
    db.commit()
    return m.id


def _write(db, tenant, obj, *, columns=None, relation=LIKES, predicate="likes", subject="Maria", **k):
    return write_fact(
        db, entities.TABLES, entities.memory_of(tenant.id, None, "u1", None),
        columns=columns or dict(tenant_id=tenant.id, project_id=None, end_user_id="u1", agent_id=None, run_id=None),
        relation=relation, is_open=true_at(), embed=lambda f: None, judge=lambda g: [],
        subject=subject, subject_type="person", predicate_raw=predicate, obj=obj, object_is_literal=False,
        confidence=0.9, source_memory_id=_memory(db, tenant), model_used=None, **k,
    )


class TestWriteFact:
    def test_a_time_without_a_zone_counts_as_utc(self, db, tenant):
        fact, _ = _write(db, tenant, "tea", valid_from=datetime(2025, 1, 1, 9, 0))
        assert _aware(fact.last_confirmed_at) == _d(2025, 1, 1, 9, 0)

    def test_the_reconfirmation_writes_the_family_that_was_missing(self, db, tenant):
        f = AgentFact(tenant_id=tenant.id, end_user_id="u1", subject_label="Maria", subject_canonical="Maria",
                      predicate_raw="likes", predicate_canonical="likes", object_label="tea", object_canonical="tea")
        db.add(f)
        db.commit()
        assert f.predicate_family is None
        same, _ = _write(db, tenant, "tea")
        assert same.id == f.id and same.predicate_family == "preferences"

    def test_in_the_past_it_reconfirms_the_closed_history(self, db, tenant):
        first, _ = _write(db, tenant, "Rome", tense="past", valid_from=_d(2024, 1, 1))
        second, _ = _write(db, tenant, "Rome", tense="past", valid_from=_d(2025, 1, 1))
        assert second.id == first.id and second.observation_count == 2

    def test_a_plan_closed_in_the_future_stays_as_it_is_if_it_comes_true_later(self, db, tenant):
        """3.0.0: the plan ends before the fact starts, the two do not
        overlap (Graphiti 2.2.3): the plan stays as it is, the fact is a
        new row from its start."""
        plan, _ = _write(db, tenant, "Lisbon", tense="planned", relation=LIKES,
                         valid_from=_d(2025, 1, 1))
        end = datetime.now(timezone.utc) + timedelta(days=30)
        plan.invalid_at = end
        db.commit()
        start = datetime.now(timezone.utc) + timedelta(days=60)
        fact, closed = _write(db, tenant, "Lisbon", valid_from=start)
        assert fact.id != plan.id and fact.tense == "current" and _aware(fact.valid_from) == _aware(start)
        assert plan.tense == "planned" and _aware(plan.valid_from) == _d(2025, 1, 1)
        assert _aware(plan.invalid_at) == _aware(end) and plan.invalidated_by_id is None
        assert plan.id not in closed

    def test_an_error_that_is_not_the_race_on_the_index_stays_an_error(self, db, tenant):
        with pytest.raises(IntegrityError):
            _write(db, tenant, "tea",
                    columns=dict(tenant_id=None, project_id=None, end_user_id="u1", agent_id=None, run_id=None))
        db.rollback()

    def test_a_failing_alias_does_not_stop_the_write(self, db, tenant, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError("alias table locked")

        monkeypatch.setattr(store, "add_alias", broken)
        fact, _ = _write(db, tenant, "Globex", relation=Relation("renamed_to", None, "identity"),
                           predicate="renamed_to", subject="Acme")
        assert fact.id is not None


# -------------------------------------------------------- resolve_predicate

class _Vocabulary:
    """Answers the four questions of the vocabulary; `fail` = which ones fail."""

    def __init__(self, *, present=None, same=None, fail=(), many=False, family="preferences"):
        self.present, self.same, self.fail = present, same, set(fail)
        self.many, self.family = many, family
        self.asked = []

    def __call__(self, prompt, think):
        if '{"past": false}' in prompt:
            type_name = "tense"
            answer = {"past": False} if self.present is None else {"past": True, "present": self.present}
        elif "SAME relation" in prompt:
            type_name, answer = "same", {"same_as": self.same}
        elif "many_values" in prompt:
            type_name, answer = "value", {"many_values": self.many}
        else:
            type_name, answer = "family", {"family": self.family}
        self.asked.append(type_name)
        if type_name in self.fail:
            raise RuntimeError("503 from the provider")
        if type_name + "_unreadable" in self.fail:
            return "not json"
        return json.dumps(answer)


def _resolve(db, tenant, phrase, voc, *, embed=None, strict=True, tense=None, severe=True, obj="tea"):
    return store.resolve_predicate(db, entities.TABLES, entities.memory_of(tenant.id, None, "u1", None), phrase,
                                      obj, ask=voc, embed=embed, strict=strict, tense=tense,
                                      severe=severe)


def _relation(db, tenant, canonical, *, centroid=None):
    r = PredicateCluster(tenant_id=tenant.id, end_user_id="u1", canonical=canonical, centroid=centroid,
                         is_functional=False,
                         family="preferences", member_count=1, example_object="tea")
    db.add(r)
    db.commit()
    return r


class TestResolvePredicate:
    def test_an_empty_phrase_is_unknown(self, db, tenant):
        rel = _resolve(db, tenant, "  ", _Vocabulary())
        assert (rel.canonical, rel.family) == ("unknown", "other")

    # The engine then caches the phrase again under the same key, in a
    # savepoint that takes the IntegrityError as "cached by a concurrent
    # write"; SQLAlchemy warns first, because the session holds the old row.
    @pytest.mark.filterwarnings("ignore:New instance <PredicateAlias:sqlalchemy.exc.SAWarning")
    def test_a_variant_that_points_to_a_relation_that_is_gone(self, db, tenant):
        # The foreign key on cluster_id would refuse the row: off for this
        # one insert, in this transaction only (see the port notes above).
        try:
            db.execute(text("SET LOCAL session_replication_role = replica"))
        except DBAPIError:
            db.rollback()
            pytest.skip("building a variant without its relation needs a superuser on the test database")
        db.add(PredicateAlias(tenant_id=tenant.id, surface="mentors", cluster_id=uuid.uuid4()))
        db.commit()
        voc = _Vocabulary()
        rel = _resolve(db, tenant, "mentors", voc)
        assert rel.canonical == "mentors" and rel.new is True

    def test_in_the_past_the_present_form_and_the_cached_variant(self, db, tenant):
        _relation(db, tenant, "uses")
        voc = _Vocabulary(present="uses")
        rel = _resolve(db, tenant, "used_to_use", voc, tense="past")
        assert rel.canonical == "uses" and voc.asked[0] == "tense"
        variant = db.query(PredicateAlias).filter(PredicateAlias.surface == "used_to_use").one()
        assert variant.cluster_id is not None
        # The second time from the cache, without questions.
        voc2 = _Vocabulary()
        assert _resolve(db, tenant, "used_to_use", voc2, tense="past").canonical == "uses"
        assert voc2.asked == []

    def test_in_the_past_the_same_form_resolves_as_it_is(self, db, tenant):
        voc = _Vocabulary(present=None)
        rel = _resolve(db, tenant, "sponsors", voc, tense="past")
        assert rel.canonical == "sponsors" and voc.asked[0] == "tense"

    @pytest.mark.parametrize("strict", [True, False])
    def test_in_the_past_the_failing_question(self, db, tenant, strict):
        voc = _Vocabulary(fail={"tense"})
        if strict:
            with pytest.raises(store.ResolutionUnavailable):
                _resolve(db, tenant, "used_to_love", voc, tense="past", strict=True)
        else:
            rel = _resolve(db, tenant, "used_to_love", voc, tense="past", strict=False)
            assert rel.resolved is False

    def test_in_the_past_the_unreadable_answer(self, db, tenant):
        voc = _Vocabulary(fail={"tense_unreadable"})
        with pytest.raises(store.ResolutionUnusable):
            _resolve(db, tenant, "used_to_love", voc, tense="past")

    def test_a_failing_vector_off_postgres_decides_nothing(self, db, tenant, monkeypatch):
        monkeypatch.setattr(store, "_pg", lambda db: False)

        def broken(text):
            raise RuntimeError("embedding service down")

        rel = _resolve(db, tenant, "cherishes", _Vocabulary(), embed=broken)
        assert rel.canonical == "cherishes" and rel.resolved is True

    def test_a_failing_vector_on_postgres_without_strictness(self, db, tenant, monkeypatch):
        monkeypatch.setattr(store, "_pg", lambda db: True)

        def broken(text):
            raise RuntimeError("embedding service down")

        rel = _resolve(db, tenant, "cherishes", _Vocabulary(), embed=broken, strict=False)
        assert rel.resolved is False
        assert db.query(PredicateCluster).filter(PredicateCluster.canonical == "cherishes").count() == 0

    def test_the_shortlist_and_the_model_that_picks_a_known_relation(self, db, tenant, monkeypatch):
        known = _relation(db, tenant, "likes", centroid=[0.1] * EMBEDDING_DIM)
        monkeypatch.setattr(store, "_pg", lambda db: True)
        monkeypatch.setattr(store, "_candidates", lambda db, R, mem, vec: [known])
        voc = _Vocabulary(same=1)
        rel = _resolve(db, tenant, "is_fond_of", voc, embed=lambda t: [0.1] * EMBEDDING_DIM)
        assert rel.canonical == "likes" and rel.new is False and voc.asked == ["same"]

    def test_the_shortlist_and_the_model_that_says_new(self, db, tenant, monkeypatch):
        known = _relation(db, tenant, "likes", centroid=[0.1] * EMBEDDING_DIM)
        monkeypatch.setattr(store, "_pg", lambda db: True)
        monkeypatch.setattr(store, "_candidates", lambda db, R, mem, vec: [known])
        voc = _Vocabulary(same=None)
        rel = _resolve(db, tenant, "collects", voc, embed=lambda t: [0.2] * EMBEDDING_DIM)
        assert rel.canonical == "collects" and rel.new is True and voc.asked[0] == "same"

    def test_the_failing_question_without_strictness(self, db, tenant):
        rel = _resolve(db, tenant, "collects", _Vocabulary(fail={"value"}), strict=False)
        assert rel.resolved is False

    def test_a_relation_with_the_same_name_and_no_variant_is_reused(self, db, tenant):
        r = _relation(db, tenant, "collects")
        rel = _resolve(db, tenant, "collects", _Vocabulary())
        assert rel.canonical == "collects"
        assert db.query(PredicateCluster).filter(PredicateCluster.canonical == "collects").count() == 1
        db.refresh(r)
        assert r.member_count == 2
