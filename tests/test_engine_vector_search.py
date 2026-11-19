"""memory_core.vector_search: the depth of the HNSW index in the searches by
meaning of the facts engine (2.12.0, BUG-320, audit of 6 October P2 n. 18).

The judge's semantic candidates, the facts near a vector that arrived
late and the twins of a write read the HNSW index with the default
depth (40) and the memory's filters after the scan: on an
index of all the accounts, fewer rows than asked, or none. On
SQLite (these tests) the setting does not exist: a fake session that
speaks as PostgreSQL checks that it is given before the query, and that SQLite
stays as it was.
"""
# Port notes: korely-agent runs only on PostgreSQL. The two tests of the branch
# off PostgreSQL keep testing it: test_on_sqlite_nothing with a session that says
# it is SQLite (the real `_pg` reads its dialect), and
# test_on_sqlite_python_distances_nearest_first on korely-agent's rows
# with the gate closed (`vector_search._pg` -> False), so the distances are Python's.
# The rest runs on korely-agent's tables (tenant_id, `facts.entities.TABLES`,
# `memory_scope`).
from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace as NS

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact
from korely_agent.services.facts import entities
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import candidates, twins, judge, thresholds, vector_search


class _Session:
    """A session that says it is PostgreSQL and records what it receives."""

    def __init__(self, version="0.8.0", fail=False, url=None):
        self.bind = NS(dialect=NS(name="postgresql"))
        self.log = []
        self.version, self.fail = version, fail
        self.url = url or f"postgresql://fake/{uuid.uuid4()}"

    def get_bind(self):
        return NS(url=self.url)

    @contextmanager
    def begin_nested(self):
        self.log.append(("savepoint",))
        yield

    def execute(self, sql):
        text = str(sql)
        self.log.append(("sql", text))
        if self.fail:
            raise RuntimeError("unrecognized configuration parameter")
        return NS(scalar=lambda: self.version)


class _Query:
    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def add_columns(self, *_):
        self.log.append(("add_columns",))
        return self

    def order_by(self, *_):
        self.log.append(("order_by",))
        return self

    def limit(self, n):
        self.log.append(("limit", n))
        return self

    def all(self):
        self.log.append(("all",))
        return self.rows


def _sql(s):
    return [r[1] for r in s.log if r[0] == "sql" and "pg_extension" not in r[1]]


class TestDepth:
    @pytest.mark.parametrize("pool, ef", [(10, 40), (200, 200), (5000, 1000)])
    def test_ef_search_follows_the_pool_between_40_and_1000(self, pool, ef):
        s = _Session()
        vector_search.hnsw_depth(s, pool)
        assert _sql(s) == [f"SET LOCAL hnsw.ef_search = {ef}", "SET LOCAL hnsw.iterative_scan = strict_order"]
        assert s.log[0] == ("savepoint",)

    def test_before_0_8_no_iterative_scan(self):
        s = _Session(version="0.7.4")
        vector_search.hnsw_depth(s, 10)
        assert _sql(s) == ["SET LOCAL hnsw.ef_search = 40"]

    def test_a_rejected_setting_does_not_stop_the_write(self):
        s = _Session(fail=True)
        vector_search.hnsw_depth(s, 10)  # does not raise

    def test_on_sqlite_nothing(self):
        seen = []
        sqlite = NS(bind=NS(dialect=NS(name="sqlite")), execute=lambda *a, **k: seen.append(a))
        vector_search.hnsw_depth(sqlite, 10)
        assert seen == []


class TestNearest:
    def test_on_postgres_the_depth_before_the_query(self):
        s = _Session()
        row = NS(id=1)
        q = _Query(s.log, [(row, 0.25)])
        assert vector_search.nearest(s, q, AgentFact.embedding, [0.1] * 768, 10) == [(row, 0.25)]
        kinds = [r[0] if r[0] != "sql" else r[1] for r in s.log]
        assert kinds.index("SET LOCAL hnsw.ef_search = 40") < kinds.index("all")
        assert ("limit", 10) in s.log

    def test_on_sqlite_python_distances_nearest_first(self, db, tenant, monkeypatch):
        monkeypatch.setattr(vector_search, "_pg", lambda db: False)

        def fact(o, v):
            f = AgentFact(tenant_id=tenant.id, end_user_id="u1", subject_label="Maria", predicate_raw="likes",
                          predicate_canonical="likes", object_label=o, embedding=v)
            db.add(f)
            return f

        near = fact("tea", [1.0, 0.0] + [0.0] * (EMBEDDING_DIM - 2))
        far = fact("coffee", [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2))
        without = fact("water", None)
        db.commit()
        rows = vector_search.nearest(db, db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id),
                               AgentFact.embedding, [0.9, 0.1] + [0.0] * (EMBEDDING_DIM - 2), 2)
        assert [r.id for r, _ in rows] == [near.id, far.id]
        assert rows[0][1] < rows[1][1]
        assert without.id not in [r.id for r, _ in rows]


class TestWhoAsksForTheNearest:
    """The three searches of the engine go through `vector_search.nearest` with their
    own pool; the semantic stage's query inside its savepoint (2.11.0)."""

    def test_the_semantic_stage_of_the_candidates(self, db, tenant, monkeypatch):
        seen = []

        def spy(db, q, column, vector, limit):
            seen.append((limit, db.in_nested_transaction()))
            return []

        monkeypatch.setattr(candidates, "_pg", lambda db: True)
        monkeypatch.setattr(candidates, "nearest", spy)
        candidates.for_the_judge(db, entities.TABLES, entities.memory_scope(tenant.id, None, "u1"),
                                 exclude_id=uuid.uuid4(), subject_label="Maria", predicate_canonical="likes",
                                 is_open=true_at(), vector=[0.1] * EMBEDDING_DIM)
        assert seen == [(candidates.SEMANTIC_LIMIT, True)]

    def test_the_twins(self, db, tenant, monkeypatch):
        seen = []
        monkeypatch.setattr(twins, "nearest", lambda db, q, c, v, limit: seen.append(limit) or [])
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1", subject_label="Maria",
                      subject_canonical="Maria", predicate_raw="likes", predicate_canonical="likes",
                      object_label="tea", object_canonical="tea", embedding=[0.1] * EMBEDDING_DIM)
        db.add(f)
        db.flush()
        twins.find_twin(db, entities.TABLES, entities.memory_of(tenant.id, None, "u1", None), f,
                              family="preferences", is_open=true_at(), thresholds=thresholds.Twin(0.1, 0.05))
        assert seen == [twins.LIMIT]

    def test_the_nearest_of_a_late_vector(self, db, tenant, monkeypatch):
        from korely_agent.services.memory_core import store

        seen = []
        monkeypatch.setattr(store, "_pg", lambda db: True)
        monkeypatch.setattr(vector_search, "nearest", lambda db, q, c, v, limit: seen.append(limit) or [])
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1", subject_label="Maria",
                      subject_canonical="Maria", predicate_raw="likes", predicate_canonical="likes",
                      object_label="tea", object_canonical="tea", tense="current")
        db.add(f)
        db.commit()
        judge.after_the_vector(db, entities.TABLES, entities.memory_scope(tenant.id, None, "u1"),
                                f, [0.1] * EMBEDDING_DIM, is_open=true_at(), single_value=lambda c: (False, None),
                                classify=lambda groups: {})
        assert seen == [candidates.SEMANTIC_LIMIT]
