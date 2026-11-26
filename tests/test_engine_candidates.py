"""memory_core.candidates: the facts the contradiction judge reads.

Since 2026-09-28 the stages that the two Agents engines had written twice,
row by row, are a single function. Here it is tested on the cloud's tables (`entities.TABLES`),
on SQLite: every stage except 1b, which is pgvector only and is tested on
Postgres in korely-agent's suite (`test_project_isolation`).

No model call.
"""
# Notes: on korely-agent's tables and on PostgreSQL (`facts.entities.TABLES`,
# `memory_scope(tenant, project, end user)` in place of the earlier judge's
# scope), so stage 1b runs too where a test passes a vector. A fact's
# `source_memory_id` is a foreign key here: the memories the first version of
# these tests named with bare uuids are real rows. The last test spies on this
# product's adapter (`facts.contradiction._stage1_candidates`).
from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts.entities import TABLES, memory_scope
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import candidates, vector_search

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _memory(db, tenant):
    m = AgentMemory(tenant_id=tenant.id, end_user_id="u1", content="x", graph_sync_status="done")
    db.add(m)
    db.commit()
    return m.id


@pytest.fixture
def add_fact(db, tenant):
    n = {"i": 0}

    def _f(s, p, o, *, end_user="u1", source=None, sources=None, closed=False, emb=None):
        n["i"] += 1
        f = AgentFact(
            id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=end_user,
            subject_label=s, subject_canonical=s, predicate_raw=p, predicate_canonical=p,
            object_label=o, object_canonical=o, source_memory_id=source,
            source_memory_ids=sources if sources is not None else ([str(source)] if source else None),
            observation_count=len(sources) if sources else 1,
            created_at=T0 + timedelta(minutes=n["i"]),
            invalid_at=T0 if closed else None, embedding=emb,
        )
        db.add(f)
        db.commit()
        return f

    return _f


def _search(db, tenant, s, p, o, *, end_user="u1", **k):
    return candidates.for_the_judge(
        db, TABLES, memory_scope(tenant.id, None, end_user),
        is_open=true_at(), exclude_id=k.pop("exclude_id", uuid.uuid4()),
        subject_label=s, predicate_canonical=p, subject_canonical=s, object_canonical=o,
        with_recall=k.pop("with_recall", end_user is not None), **k,
    )


def _objects(rows):
    return [r.object_label for r in rows]


class TestTheStages:
    def test_1a_same_subject_ignoring_case(self, db, tenant, add_fact):
        add_fact("Maria", "works_at", "Apple")
        add_fact("Luca", "works_at", "Initech")
        got = _search(db, tenant, "maria", "works_at", "Google", with_recall=False)
        assert _objects(got) == ["Apple"]

    def test_1a_the_opposite_family(self, db, tenant, add_fact):
        add_fact("Maria", "likes", "fish")
        got = _search(db, tenant, "Maria", "dislikes", "fish", with_recall=False)
        assert _objects(got) == ["fish"]

    def test_1c_same_subject_compatible_object_other_relation(self, db, tenant, add_fact):
        add_fact("Gordon", "lacks", "SSO support")
        add_fact("Gordon", "lacks", "dark mode")
        got = _search(db, tenant, "Gordon", "adopted", "SSO", with_recall=False)
        assert _objects(got) == ["SSO support"]

    def test_1d_the_reverse_relation(self, db, tenant, add_fact):
        add_fact("Salesforce", "serves_as_crm_for", "Globex")
        got = _search(db, tenant, "Globex", "no_longer_uses", "Salesforce", with_recall=False)
        assert _objects(got) == ["Globex"]

    def test_only_arguments_leaves_out_the_same_relation(self, db, tenant, add_fact):
        add_fact("Maria", "works_at", "Apple")          # 1a: out
        add_fact("Maria", "left_company", "Apple Inc")  # opposite family, compatible object
        add_fact("Maria", "visited", "Apple")           # 1c: in
        got = _search(db, tenant, "Maria", "works_at", "Apple", only_arguments=True)
        # 1c, newest first; the works_at itself is not there
        assert _objects(got) == ["Apple", "Apple Inc"]

    def test_1a_bis_only_within_an_end_user(self, db, tenant, add_fact):
        add_fact("the runner", "personal_best", "21 minutes")
        recalled = _search(db, tenant, "<user>", "personal_best", "19 minutes", with_recall=True)
        not_recalled = _search(db, tenant, "<user>", "personal_best", "19 minutes", with_recall=False)
        assert _objects(recalled) == ["21 minutes"] and not_recalled == []

    def test_the_order_the_judge_reads(self, db, tenant, add_fact):
        """The most precise signal first: 1a, 1c, 1d, 1a-bis."""
        r = add_fact("someone", "works_at", "Umbrella")      # 1a-bis
        d = add_fact("Apple", "employs", "Maria")            # 1d
        c = add_fact("Maria", "visited", "Google")           # 1c
        a = add_fact("Maria", "works_at", "Apple")           # 1a
        got = _search(db, tenant, "Maria", "works_at", "Google")
        # "Apple employs Maria" is not the reverse relation of Maria/Google
        assert [x.id for x in got] == [a.id, c.id, r.id]
        assert d.id not in [x.id for x in got]


class TestWhoDoesNotEnter:
    def test_the_new_fact_itself(self, db, tenant, add_fact):
        f = add_fact("Maria", "works_at", "Apple")
        assert _search(db, tenant, "Maria", "works_at", "Google", exclude_id=f.id) == []

    def test_a_closed_fact(self, db, tenant, add_fact):
        add_fact("Maria", "works_at", "Apple", closed=True)
        assert _search(db, tenant, "Maria", "works_at", "Google") == []

    def test_another_end_user(self, db, tenant, add_fact):
        add_fact("Maria", "works_at", "Apple", end_user="u2")
        assert _search(db, tenant, "Maria", "works_at", "Google") == []

    def test_bug_123_the_same_memory_even_as_a_reconfirmation(self, db, tenant, add_fact):
        m, other = _memory(db, tenant), _memory(db, tenant)
        add_fact("Maria", "works_at", "Apple", source=m)
        add_fact("Maria", "works_at", "IBM", source=other, sources=[str(other), str(m)])
        add_fact("Maria", "works_at", "Initech", source=other)
        got = _search(db, tenant, "Maria", "works_at", "Google", source=m)
        assert _objects(got) == ["Initech"]


class TestNoFilterOnObjects:
    """The 0.20 filter of the self-hosted was measured and removed on
    2026-09-28 ("Globex uses Jira" -> "uses Linear" is 0.217 away: a true
    replacement that would never have reached the judge)."""

    def test_cosine(self):
        # `vector_search.cosine` since 2.12.0; its name in `candidates` left with 4.0.0.
        assert vector_search.cosine([1, 0], [1, 0]) == pytest.approx(0.0)
        assert vector_search.cosine([1, 0], [0, 1]) == pytest.approx(1.0)
        assert vector_search.cosine([0, 0], [1, 0]) is None
        assert vector_search.cosine(None, [1, 0]) is None

    def test_a_far_object_reaches_the_judge(self, db, tenant, add_fact):
        add_fact("Maria", "likes", "iPhone", emb=[0.0] * (EMBEDDING_DIM - 1) + [1.0])
        got = _search(db, tenant, "Maria", "likes", "fish", with_recall=False,
                     vector=[1.0] + [0.0] * (EMBEDDING_DIM - 1))
        assert _objects(got) == ["iPhone"]
        assert "objects_filter" not in inspect.signature(candidates.for_the_judge).parameters


def test_the_cloud_adapter_calls_the_common_function(db, tenant, add_fact, monkeypatch):
    """`_stage1_candidates` stays the cloud's seam (the tests replace it) and
    passes the scope, `true_at()` and the recall only with an end user."""
    from korely_agent.services.facts import contradiction

    seen = {}

    def _spy(db, tables, scope, **k):
        seen.update(k, tables=tables)
        return []

    monkeypatch.setattr(candidates, "for_the_judge", _spy)
    contradiction._stage1_candidates(db, tenant.id, None, "Maria", "works_at",
                                     exclude_id=uuid.uuid4(), object_canonical="Google", agent_id=None)
    assert seen["tables"] is TABLES and seen["with_recall"] is False
    contradiction._stage1_candidates(db, tenant.id, "u1", "Maria", "works_at",
                                     exclude_id=uuid.uuid4(), agent_id=None)
    assert seen["with_recall"] is True


class TestWithoutThePairsOfOpposites:
    """3.3.0 (the rule of 8 Oct: no word lists in the code): stage 1a
    is the same relation only; two relations that exclude each other share
    their arguments, and stage 1c brings them."""

    def test_another_object_of_the_opposite_relation_does_not_enter(self, db, tenant, add_fact):
        add_fact("Maria", "likes", "broccoli")
        got = _search(db, tenant, "Maria", "dislikes", "fish", with_recall=False)
        assert _objects(got) == []

    def test_the_same_arguments_enter(self, db, tenant, add_fact):
        add_fact("Maria", "likes", "broccoli")
        add_fact("Maria", "likes", "fish")
        got = _search(db, tenant, "Maria", "dislikes", "fish", with_recall=False)
        assert _objects(got) == ["fish"]

    @pytest.mark.parametrize("old,new", [
        ("likes", "dislikes"), ("married_to", "divorced_from"), ("works_at", "left_company"),
        ("lives_in", "moved_from"), ("friend_of", "estranged_from"),
    ])
    def test_the_five_pairs_of_before_come_from_stage_1c(self, db, tenant, add_fact, old, new):
        add_fact("Maria", old, "Acme")
        got = _search(db, tenant, "Maria", new, "Acme", with_recall=False)
        assert [(r.predicate_canonical, r.object_label) for r in got] == [(old, "Acme")]

    def test_no_list_in_the_module(self):
        from korely_agent.services.memory_core import families

        assert not hasattr(families, "OPPOSING_PREDICATE_FAMILIES")
        assert "without_lists" not in inspect.signature(candidates.for_the_judge).parameters
