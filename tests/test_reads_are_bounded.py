"""No request or worker pass loads a whole table into the one API process
(audit of 20 Oct 2026).

The API is one process, with the worker inside it, under one memory limit,
for every account of the install. Each test seeds more rows than the read
needs and watches every statement the database answers: none may return a
row per stored row (the page, the groups or the count are the database's),
and none may select a vector or a stored batch payload it does not use
(db/bounded.py).
"""
import contextlib
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

from korely_agent.db.models import (AgentBatchJob, AgentFact, AgentMemory, AuditEvent,
                                    PredicateAlias, PredicateCluster)
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service
from tests.conftest import seeded_project_id

# More rows than any statement below may return: a statement that returns
# this many returned one per stored row.
MANY = 60
VECTOR = [0.125] * 768
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


@contextlib.contextmanager
def statements():
    """Every statement any engine runs meanwhile, with the rows it returned
    (a server-side cursor, a streamed read, reports none at once)."""
    seen = []

    def after(conn, cursor, statement, parameters, context, executemany):
        rows = cursor.rowcount if cursor.description is not None else 0
        seen.append((statement, max(rows, 0)))

    event.listen(Engine, "after_cursor_execute", after)
    try:
        yield seen
    finally:
        event.remove(Engine, "after_cursor_execute", after)


def most_rows(seen) -> int:
    return max((n for _s, n in seen), default=0)


def selected(seen, column: str) -> list:
    """The SELECTs whose column list names `column` ("agent_facts.embedding")."""
    named = re.compile(re.escape(column) + r"(?![\w])")
    return [s for s, _n in seen
            if s.lstrip().upper().startswith("SELECT") and named.search(s.split(" FROM ")[0])]


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _bearer(db, tenant, scopes):
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name="k", scopes=list(scopes))
    return {"Authorization": f"Bearer {full}"}


def read_key(db, tenant):
    return _bearer(db, tenant, ["memories:read"])


def operator_key(db, tenant):
    return _bearer(db, tenant, list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])


def _memory(tenant, project, i, **kw):
    return AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project,
                       content=f"memory {i}", created_at=NOW + timedelta(minutes=i), **kw)


def _fact(tenant, project, i, **kw):
    kw.setdefault("end_user_id", "u1")
    kw.setdefault("subject_label", f"s{i}")
    kw.setdefault("valid_from", NOW)
    return AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project,
                     subject_canonical=f"s{i}",
                     predicate_raw="likes", predicate_canonical=kw.pop("predicate", "likes"),
                     object_label=f"o{i}", object_canonical=f"o{i}", confidence=0.9,
                     created_at=NOW + timedelta(minutes=i), **kw)


# ── the Markdown export loaded whole fact rows ──────────────────────────────

class TestTheExport:
    def test_it_reads_the_text_columns_a_batch_at_a_time(self, db, tenant, tmp_path, monkeypatch):
        from korely_agent.services import export_md

        project = seeded_project_id(db, tenant.id)
        db.add_all([_fact(tenant, project, i, embedding=VECTOR,
                          invalid_at=NOW + timedelta(days=1) if i % 3 == 0 else None)
                    for i in range(MANY)])
        db.commit()
        try:
            from korely_agent.db import bounded
        except ImportError:  # before db/bounded.py
            bounded = None
        if bounded is not None:
            monkeypatch.setattr(bounded, "STREAM_ROWS", 7)
        with statements() as seen:
            assert export_md.export_tenant(db, tenant.id, tmp_path) == 1
        assert selected(seen, "agent_facts.embedding") == [], "the export loaded the vectors"
        assert most_rows(seen) < MANY, "the export loaded every fact of the end user at once"

    def test_the_file_is_what_render_writes(self, db, tenant, tmp_path):
        """The same bytes as before: the order is the database's now."""
        from korely_agent.services import export_md

        project = seeded_project_id(db, tenant.id)
        rows = [_fact(tenant, project, i, subject_label=label, valid_from=NOW - timedelta(days=i % 4),
                      invalid_at=(NOW + timedelta(days=i)) if i % 2 else None)
                for i, label in enumerate(["b", "a", "B", "é", "a|b", "Z", "a", "ß"])]
        rows.append(_fact(tenant, project, 99, subject_label="x", valid_from=NOW, end_user_id="u1"))
        db.add_all(rows)
        db.commit()
        export_md.export_tenant(db, tenant.id, tmp_path)
        got = (tmp_path / str(tenant.id) / "u1.md").read_text(encoding="utf-8")
        db.expire_all()
        assert got == export_md.render("u1", db.query(AgentFact).filter(
            AgentFact.tenant_id == tenant.id).all())


# ── the requests summary was read whole ─────────────────────────────────────

class TestTheRequestSummary:
    def test_the_database_counts_the_trail(self, db, tenant, client):
        from korely_agent.services import audit

        db.add_all([AuditEvent(tenant_id=tenant.id, actor="rest",
                               action=audit.READ if i % 2 else audit.WRITE,
                               result="ok" if i % 5 else "error",
                               meta={"endpoint": f"{'r' if i % 2 else 'w'}{i % 3}"},
                               ts=datetime.now(timezone.utc))
                    for i in range(MANY)])
        db.commit()
        key = operator_key(db, tenant)
        with statements() as seen:
            r = client.get("/admin/requests/summary", headers=key)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == MANY and body["reads"] == MANY // 2 and body["refused"] == MANY // 5
        want = {}
        for i in range(MANY):
            name = f"{'r' if i % 2 else 'w'}{i % 3}"
            want[(name, "read" if i % 2 else "write")] = want.get((name, "read" if i % 2 else "write"), 0) + 1
        assert {(e["endpoint"], e["kind"]): e["count"] for e in body["by_endpoint"]} == want
        assert most_rows(seen) < MANY, "the summary read every row of the trail"


# ── the memory categories were read whole ───────────────────────────────────

class TestTheMemoryCategories:
    def test_the_database_counts_the_memories(self, db, tenant, client):
        project = seeded_project_id(db, tenant.id)
        mems = [_memory(tenant, project, i) for i in range(MANY // 2)]
        db.add_all(mems)
        db.commit()
        # Two facts per memory, two predicates of one family on half of them.
        db.add_all([_fact(tenant, project, i, source_memory_id=mems[i // 2].id,
                          predicate="lives_in" if i % 2 else "works_at")
                    for i in range(MANY)])
        db.commit()
        key = operator_key(db, tenant)
        with statements() as seen:
            r = client.get("/admin/memories/categories", headers=key)
        assert r.status_code == 200, r.text
        from korely_agent.services.memory_core.families import family_of_the_fact

        want = {}
        for i in range(MANY):
            fam = family_of_the_fact(None, "lives_in" if i % 2 else "works_at") or "other"
            want.setdefault(fam, set()).add(i // 2)
        assert {c["family"]: c["count"] for c in r.json()} == {f: len(v) for f, v in want.items()}
        assert most_rows(seen) < MANY // 2, "the categories read a row per live fact"


# ── the activity listing was paged in Python ────────────────────────────────

class TestTheListings:
    @pytest.fixture
    def seeded(self, db, tenant):
        project = seeded_project_id(db, tenant.id)
        # Each end user and agent its own memory, newest last; a-000's is
        # forgotten (its fact is live), and zz-gone has only a forgotten
        # memory, so it is listed last, with zeros.
        db.add_all([_memory(tenant, project, i, end_user_id=f"u-{i:03}", agent_id=f"a-{i:03}",
                            deleted_at=NOW if i == 0 else None) for i in range(MANY)])
        db.add(_memory(tenant, project, MANY, agent_id="zz-gone", deleted_at=NOW))
        db.add_all([_fact(tenant, project, i, end_user_id=f"u-{i:03}", agent_id=f"a-{i:03}")
                    for i in range(0, MANY, 2)])
        db.commit()
        return project

    def test_v1_users_is_paged_by_the_database(self, db, tenant, client, seeded):
        key = read_key(db, tenant)
        with statements() as seen:
            r = client.get("/v1/users?limit=5&offset=3", headers=key)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == MANY
        assert [u["user_id"] for u in body["users"]] == [f"u-{i:03}" for i in range(MANY - 4, MANY - 9, -1)]
        assert most_rows(seen) <= 5, "the listing read every end user to show five"

    def test_v1_agents_is_paged_by_the_database(self, db, tenant, client, seeded):
        key = read_key(db, tenant)
        with statements() as seen:
            r = client.get(f"/v1/agents?limit=5&offset={MANY - 2}", headers=key)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == MANY + 1
        # The last page: the oldest active agents, then the one with only a
        # forgotten memory, at the end with zeros.
        assert [(a["agent_id"], a["memories"], a["facts"]) for a in body["agents"]] == [
            ("a-001", 1, 0), ("a-000", 0, 1), ("zz-gone", 0, 0)]
        assert most_rows(seen) <= 5, "the listing read every agent to show five"

    def test_the_admin_namespaces_take_a_page(self, db, tenant, client, seeded):
        key = operator_key(db, tenant)
        with statements() as seen:
            r = client.get("/admin/namespaces?limit=4&offset=0", headers=key)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == MANY - 1  # a-000's only memory is forgotten
        assert len(body["agents"]) == 4
        assert most_rows(seen) <= 4, "the namespaces read every agent of the account"

    def test_deleting_an_agent_asks_for_that_agent_only(self, db, tenant, client, seeded):
        key = _bearer(db, tenant, ["memories:read", "memories:write"])
        with statements() as seen:
            r = client.delete("/v1/agents/no-such-agent", headers=key)
        assert r.status_code == 404
        assert most_rows(seen) <= 1, "the check read every agent id of the project"


# ── a project's deletion moved whole rows ───────────────────────────────────

class TestDeletingAProject:
    @pytest.fixture
    def default(self, db, tenant):
        return project_service.get_or_create_default_project(db, tenant.id)

    @pytest.fixture
    def gamma(self, db, tenant, default):
        return project_service.create_project(db, tenant_id=tenant.id, name="Gamma")

    def test_the_fold_reads_only_the_collisions(self, db, tenant, default, gamma):
        db.add_all([_fact(tenant, gamma.id, i, embedding=VECTOR) for i in range(MANY)])
        # Two of them are already open in the default project.
        db.add_all([_fact(tenant, default.id, i, embedding=VECTOR) for i in (5, 9)])
        db.commit()
        with statements() as seen:
            out = project_service.delete_project(db, tenant.id, gamma)
        assert out["facts_merged"] == 2
        assert selected(seen, "agent_facts.embedding") == [], "the move loaded the vectors"
        facts_reads = [s for s, _n in seen if s.lstrip().upper().startswith("SELECT")
                       and "FROM agent_facts" in s]
        assert len(facts_reads) < MANY, "one read of the facts per moved fact"
        assert most_rows(seen) < MANY

    def test_an_empty_project_reads_nothing_of_the_default_vocabulary(self, db, tenant, default, gamma):
        clusters = [PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id, project_id=default.id,
                                     end_user_id=f"u{i}", canonical="likes", centroid=VECTOR)
                    for i in range(MANY)]
        db.add_all(clusters)
        db.flush()
        db.add_all([PredicateAlias(tenant_id=tenant.id, project_id=default.id, end_user_id=c.end_user_id,
                                   surface="likes", cluster_id=c.id) for c in clusters])
        db.commit()
        with statements() as seen:
            project_service.delete_project(db, tenant.id, gamma)
        assert selected(seen, "predicate_clusters.centroid") == []
        assert most_rows(seen) < MANY, "the move read the default project's whole vocabulary"

    def test_the_vocabulary_still_merges(self, db, tenant, default, gamma):
        """The set-based merge does what the loop did: a relation the end user
        has in both keeps the default's row and gets the phrases, a phrase
        already resolved there goes, any other relation moves whole."""
        def cluster(project, user, canonical):
            c = PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project,
                                 end_user_id=user, canonical=canonical)
            db.add(c)
            db.flush()
            return c

        def alias(c, surface):
            db.add(PredicateAlias(tenant_id=tenant.id, project_id=c.project_id,
                                  end_user_id=c.end_user_id, surface=surface, cluster_id=c.id))

        theirs = cluster(default.id, "u1", "likes")
        alias(theirs, "likes")
        twin = cluster(gamma.id, "u1", "likes")
        alias(twin, "likes")          # resolved in the default already: goes
        alias(twin, "is fond of")     # joins the default's relation
        nulls = cluster(default.id, None, "owns")
        alias(nulls, "owns")
        null_twin = cluster(gamma.id, None, "owns")
        alias(null_twin, "has")       # the end user of no end user matches too
        alone = cluster(gamma.id, "u2", "likes")
        alias(alone, "likes")         # another end user's: moves whole
        db.commit()
        project_service.delete_project(db, tenant.id, gamma)
        db.expire_all()
        rows = {(c.end_user_id, c.canonical): c for c in db.query(PredicateCluster)
                .filter(PredicateCluster.tenant_id == tenant.id)}
        assert set(rows) == {("u1", "likes"), (None, "owns"), ("u2", "likes")}
        assert {c.project_id for c in rows.values()} == {default.id}
        assert rows[("u1", "likes")].id == theirs.id and rows[(None, "owns")].id == nulls.id
        phrases = {(a.end_user_id, a.surface): a.cluster_id for a in db.query(PredicateAlias)
                   .filter(PredicateAlias.tenant_id == tenant.id)}
        assert phrases == {("u1", "likes"): theirs.id, ("u1", "is fond of"): theirs.id,
                           (None, "owns"): nulls.id, (None, "has"): nulls.id,
                           ("u2", "likes"): alone.id}
        assert {a.project_id for a in db.query(PredicateAlias)} == {default.id}


# ── a batch job's scrub loaded every payload (the residual) ────────────────

class TestABatchJob:
    @pytest.fixture
    def job(self, db, tenant):
        project = seeded_project_id(db, tenant.id)
        j = AgentBatchJob(tenant_id=tenant.id, project_id=project, status="pending",
                          received=3, payload=[{"content": "x" * 1000}] * 3)
        db.add(j)
        db.commit()
        return j

    def test_its_status_reads_no_payload(self, db, tenant, job):
        from korely_agent.services import batch

        db.expunge_all()
        with statements() as seen:
            out = batch.job_status(db, tenant.id, job.id)
        assert out["received"] == 3
        assert [s for s, _n in seen if "agent_batch_jobs.payload" in s] == []

    def test_its_claim_reads_no_payload(self, db, tenant, job, monkeypatch):
        from korely_agent.services import batch

        monkeypatch.setattr(batch, "_import_one", lambda *a, **k: (None, "stub"))
        db.expunge_all()
        with statements() as seen:
            batch.process_batch_jobs(db, limit=5)
        assert any("SKIP LOCKED" in s for s, _n in seen), "nothing was claimed"
        assert [s for s, _n in seen if "AS agent_batch_jobs_payload" in s] == [], \
            "the claim or the drain read a whole payload"
