"""memory_core 2.11.0: the reconfirmation that did not find the fact, and the
stages that failed without a savepoint.

From the code audit of 6 Oct 2026: `store.find_open` and the unique index on
the open triple used different predicates. The index lowercases with the
database's `lower()`, the read with Python's ("İstanbul": nine characters in
Python, eight in PostgreSQL, unchanged in SQLite); the index counts "invalid_at
IS NULL", the recovery after the race "true now" (`true_at()`), which leaves out
a fact with valid_from in the future. In both cases the insert hit the index,
the recovery did not find the winner and the write failed every time the fact
was repeated.

From an audit on 2.10.0: the semantic stage of the candidates
and the vocabulary shortlist failed inside an `except` without a savepoint (on
Postgres the transaction stayed aborted), and the failure counted as "no
candidate"; `cosine` swallowed every exception.

SQLite: the unique index holds only with project, end user and agent not null
(distinct NULLs), as here. No model.
"""
# Port notes: on korely-agent's tables (`AgentProject`, `AgentFact`, tenant_id,
# `facts.entities.TABLES`) and on PostgreSQL, where the unique index on the open
# triple (NULLS NOT DISTINCT) and `lower()` are the database's own: the two
# reconfirmation cases run against the real index. The semantic stage's real
# failed statement was, on SQLite, the missing pgvector operator; on
# PostgreSQL it is the same query given a vector PostgreSQL refuses (NaN:
# "NaN not allowed in vector", a DataError raised by the database inside the
# stage's savepoint).
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentProject, PredicateAlias, PredicateCluster
from korely_agent.services.facts import entities
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import store, candidates, vector_search
from korely_agent.services.memory_core.store import Relation
from korely_agent.services.memory_core.writing import write_fact


def _project(db, tenant):
    p = AgentProject(tenant_id=tenant.id, name=f"P {uuid.uuid4().hex[:6]}", slug=uuid.uuid4().hex[:10])
    db.add(p)
    db.commit()
    return p.id


def _write(db, tenant, project, obj, *, valid_from=None):
    fact, closed = write_fact(
        db, entities.TABLES, entities.memory_of(tenant.id, project, "u1", "a1"),
        columns=dict(tenant_id=tenant.id, project_id=project, end_user_id="u1", agent_id="a1", run_id=None),
        relation=Relation(canonical="lives_in", is_functional=None, family="places"),
        is_open=true_at(), embed=lambda f: None, judge=lambda g: [],
        subject="Maria", subject_type="person", predicate_raw="lives in", obj=obj,
        object_is_literal=False, confidence=0.9, source_memory_id=None, model_used=None,
        valid_from=valid_from,
    )
    db.commit()
    return fact


class TestTheReconfirmation:
    def test_a_name_that_python_and_the_database_lowercase_differently(self, db, tenant):
        project = _project(db, tenant)
        first = _write(db, tenant, project, "İstanbul")
        second = _write(db, tenant, project, "İstanbul")
        assert second.id == first.id and second.observation_count == 2
        assert db.query(AgentFact).count() == 1

    def test_an_open_fact_with_valid_from_in_the_future(self, db, tenant):
        """A timestamp in the future (a local time without a zone read as UTC):
        the fact is in the index but is not true now. Repeating it reconfirms it."""
        project = _project(db, tenant)
        in_two_hours = datetime.now(timezone.utc) + timedelta(hours=2)
        first = _write(db, tenant, project, "Rome", valid_from=in_two_hours)
        second = _write(db, tenant, project, "Rome")
        assert second.id == first.id and second.observation_count == 2

    def test_find_open_lowercases_on_the_same_side_as_the_index(self, db, tenant):
        project = _project(db, tenant)
        first = _write(db, tenant, project, "İstanbul")
        found = store.find_open(db, entities.TABLES, entities.memory_of(tenant.id, project, "u1", "a1"),
                                        "Maria", "lives_in", "İstanbul")
        assert found is not None and found.id == first.id


class TestTheSemanticStageOfTheCandidates:
    def test_an_error_is_not_no_candidate_and_the_transaction_stays_alive(self, db, tenant):
        """On SQLite the pgvector operator does not exist: a real failed
        statement, inside the savepoint. Since 2.12.0 the query runs in
        `vector_search.nearest`, which computes the distances in Python off
        PostgreSQL: it is told it is on PostgreSQL too (its depth setting
        fails as well, in a savepoint of its own, and is only logged)."""
        with pytest.raises(candidates.CandidatesUnread):
            candidates.for_the_judge(
                db, entities.TABLES, entities.memory_scope(tenant.id, None, "u1"),
                exclude_id=uuid.uuid4(), subject_label="Maria", predicate_canonical="lives_in",
                is_open=true_at(), vector=[float("nan")] * EMBEDDING_DIM,
            )
        assert db.query(AgentFact).count() == 0, "the transaction goes on"


class TestTheVocabularyShortlist:
    def _resolve(self, db, tenant, *, strict):
        return store.resolve_predicate(
            db, entities.TABLES, entities.memory_of(tenant.id, None, "u1", None), "lives_in", "Rome",
            ask=lambda prompt, think: '{"same_as": null}', embed=lambda text: [0.1] * EMBEDDING_DIM,
            strict=strict,
        )

    def _break(self, monkeypatch):
        def _candidates(db, R, mem, vec):
            raise RuntimeError("canceling statement due to statement timeout")

        monkeypatch.setattr(store, "_pg", lambda db: True)
        monkeypatch.setattr(store, "_candidates", _candidates)

    def test_strict_is_a_failed_resolution(self, db, tenant, monkeypatch):
        self._break(monkeypatch)
        with pytest.raises(store.ResolutionUnavailable):
            self._resolve(db, tenant, strict=True)
        assert db.query(PredicateCluster).count() == 0 and db.query(PredicateAlias).count() == 0

    def test_without_strictness_the_surface_form_without_cache(self, db, tenant, monkeypatch):
        self._break(monkeypatch)
        rel = self._resolve(db, tenant, strict=False)
        assert rel.canonical == "lives_in" and rel.resolved is False
        assert db.query(PredicateCluster).count() == 0, "never cached as a new relation"


class TestCosine:
    # `vector_search.cosine` since 2.12.0; its name in `candidates` left with 4.0.0.
    def test_a_vector_that_is_not_numbers_is_none(self):
        assert vector_search.cosine([1.0, "a"], [1.0, 2.0]) is None
        assert vector_search.cosine(None, [1.0]) is None
        assert vector_search.cosine([0.0, 0.0], [1.0, 1.0]) is None

    def test_a_defect_is_not_hidden(self):
        class Broken:
            def __iter__(self):
                raise RuntimeError("a defect")

        with pytest.raises(RuntimeError):
            vector_search.cosine(Broken(), [1.0])
