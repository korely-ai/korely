"""What a read returned, in the audit trail (bussola 2e, 2026-10-01).

The demo question: "who wrote this memory, and what did the agent know when it
decided?". The trail recorded that a read happened and not what it returned,
and four calls (memories:search, :get, :update, :delete) did not even say
whose memory they touched. Now a read keeps the public ids of the memories and
the facts it returned (at most 100 of each, the counts in `meta`), every call
says its end user and the memory it touched, GET /v1/audit returns them as
`read`, and the operator's Audit log turns them into text on demand, from the
live tables. The same column and field as the hosted product.

Never content: only ids, and the text the screen shows is read now, so an
erased memory comes back as erased.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentFact, AgentMemory, AuditEvent, Tenant
from korely_agent.services import audit
from korely_agent.services import keys as key_service
from korely_agent.services.ids import fact_public_id, mem_public_id
from tests.conftest import seeded_project_id


NOW = datetime.now(timezone.utc)


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


@pytest.fixture
def auth(db, tenant):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="t")
    return {"Authorization": f"Bearer {full}"}


@pytest.fixture
def admin(db, tenant):
    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="operator",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    return {"Authorization": f"Bearer {full}"}


def memory(db, tenant, text="Maria lives in Milan.", eu="maria"):
    m = AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                    end_user_id=eu, content=text, graph_sync_status="done")
    db.add(m)
    db.commit()
    return m


def fact(db, tenant, m, obj="Milan", closed=False):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=m.project_id,
                  end_user_id=m.end_user_id, subject_label="Maria", subject_canonical="maria",
                  predicate_raw="lives_in", predicate_canonical="lives_in",
                  object_label=obj, object_canonical=obj.lower(), confidence=0.9,
                  valid_from=NOW - timedelta(days=10),
                  invalid_at=NOW - timedelta(days=1) if closed else None,
                  source_memory_id=m.id, source_memory_ids=[str(m.id)])
    db.add(f)
    db.commit()
    return f


def last(db, tenant, endpoint):
    db.expire_all()
    rows = (db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id)
              .order_by(AuditEvent.ts.desc()).all())
    return next(r for r in rows if (r.meta or {}).get("endpoint") == endpoint)


class TestTheRecord:
    def test_public_ids_at_most_a_hundred_and_the_count(self, db, tenant):
        mems = [uuid.uuid4() for _ in range(150)]
        audit.record(db, tenant.id, audit.READ, memories_read=mems,
                     facts_read=[uuid.uuid4(), uuid.uuid4()], meta={"endpoint": "x"})
        row = last(db, tenant, "x")
        assert len(row.read_ids["memories"]) == audit.MAX_IDS
        assert row.read_ids["memories"][0] == mem_public_id(mems[0])
        assert all(i.startswith("fct_") for i in row.read_ids["facts"])
        assert row.meta["memories_read"] == 150 and row.meta["facts_read"] == 2

    def test_a_read_that_found_nothing_says_so(self, db, tenant):
        audit.record(db, tenant.id, audit.READ, memories_read=[], meta={"endpoint": "y"})
        row = last(db, tenant, "y")
        assert row.read_ids == {"memories": []} and row.meta["memories_read"] == 0

    def test_an_event_that_read_nothing_has_no_read(self, db, tenant):
        audit.record(db, tenant.id, audit.WRITE, meta={"endpoint": "z"})
        assert last(db, tenant, "z").read_ids is None


class TestEveryReadSaysWhatItReturned:
    def test_get_a_memory(self, db, tenant, client, auth):
        m = memory(db, tenant)
        now, then = fact(db, tenant, m), fact(db, tenant, m, "Rome", closed=True)
        assert client.get(f"/v1/memories/{mem_public_id(m.id)}", headers=auth).status_code == 200
        row = last(db, tenant, "memories:get")
        assert row.end_user_id == "maria" and row.target_id == mem_public_id(m.id)
        assert row.read_ids == {"memories": [mem_public_id(m.id)],
                                "facts": [fact_public_id(now.id)]}, "the closed fact was not shown"
        assert then is not None

    def test_update_and_delete_say_whose_memory(self, db, tenant, client, auth, monkeypatch):
        monkeypatch.setattr("korely_agent.services.memory_engine.embed", lambda *a, **k: None)
        m = memory(db, tenant)
        r = client.patch(f"/v1/memories/{mem_public_id(m.id)}", headers=auth,
                         json={"content": "Maria lives in Turin."})
        assert r.status_code == 200, r.text
        up = last(db, tenant, "memories:update")
        assert (up.end_user_id, up.target_id) == ("maria", mem_public_id(m.id))
        assert client.delete(f"/v1/memories/{mem_public_id(m.id)}", headers=auth).status_code == 200
        gone = last(db, tenant, "memories:delete")
        assert (gone.end_user_id, gone.target_id) == ("maria", mem_public_id(m.id))

    def test_search(self, db, tenant, client, auth, monkeypatch):
        m = memory(db, tenant)
        from korely_agent.services.memory_engine import AgentMemoryEngine

        monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, *a, **k: [
            {"id": str(m.id), "content": m.content, "score": 0.9, "end_user_id": "maria"}])
        r = client.post("/v1/memories/search", headers=auth,
                        json={"query": "where does Maria live", "user_id": "maria"})
        assert r.status_code == 200, r.text
        row = last(db, tenant, "memories:search")
        assert row.end_user_id == "maria"
        assert row.read_ids == {"memories": [mem_public_id(m.id)]}
        assert "Maria" not in str(row.meta), "the query is content"

    def test_the_context_records_what_it_handed_the_agent(self, db, tenant, client, auth, monkeypatch):
        m = memory(db, tenant)
        f = fact(db, tenant, m)
        monkeypatch.setattr("korely_agent.api.context.assemble_context", lambda *a, **k: {
            "context": "...", "tokens": 1, "degraded": False, "degraded_parts": [],
            "sources": [fact_public_id(f.id), mem_public_id(m.id)]})
        assert client.get("/v1/context", headers=auth,
                          params={"query": "where", "user_id": "maria"}).status_code == 200
        row = last(db, tenant, "context")
        assert row.end_user_id == "maria"
        assert row.read_ids == {"memories": [mem_public_id(m.id)], "facts": [fact_public_id(f.id)]}

    def test_the_facts_list(self, db, tenant, client, auth):
        m = memory(db, tenant)
        f = fact(db, tenant, m)
        r = client.get("/v1/facts", headers=auth, params={"user_id": "maria"})
        assert r.status_code == 200, r.text
        row = last(db, tenant, "facts:list")
        assert row.end_user_id == "maria" and row.read_ids == {"facts": [fact_public_id(f.id)]}


class TestTheFactWritesSayWhoseFact:
    """Found on the live page (2026-10-01): a fact written by hand was missing
    from the end user's trail, because POST, DELETE and PATCH /v1/facts wrote
    neither the end user nor the fact."""

    def test_write_correct_and_forget(self, db, tenant, client, auth, monkeypatch):
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
        monkeypatch.setattr("korely_agent.services.facts.service._embed_fact", lambda *a, **k: None)
        r = client.post("/v1/facts", headers=auth, json={
            "user_id": "maria", "subject": "Maria", "predicate": "lives_in", "object": "Milan"})
        assert r.status_code in (200, 201), r.text
        fid = r.json()["id"]
        made = last(db, tenant, "facts:create")
        assert (made.end_user_id, made.target_id) == ("maria", fid)

        r = client.patch(f"/v1/facts/{fid}", headers=auth, json={"object": "Turin"})
        assert r.status_code == 200, r.text
        fixed = last(db, tenant, "facts:correct")
        assert (fixed.end_user_id, fixed.target_id) == ("maria", fid)

        new_id = r.json()["id"]
        assert client.post(f"/v1/facts/{new_id}/forget", headers=auth, json={}).status_code == 200
        gone = last(db, tenant, "facts:forget")
        assert (gone.end_user_id, gone.target_id) == ("maria", new_id)


class TestTheTrailSaysIt:
    def test_v1_audit_returns_read(self, db, tenant, client, auth):
        m = memory(db, tenant)
        client.get(f"/v1/memories/{mem_public_id(m.id)}", headers=auth)
        events = client.get("/v1/audit", headers=auth, params={"user_id": "maria"}).json()["events"]
        got = next(e for e in events if e["meta"]["endpoint"] == "memories:get")
        # Every kind the answer has, the core's and a part's
        # (core/extensions.Part.extend), empty but the memory read.
        from korely_agent.api.audit import AuditRead

        assert got["read"] == {**{kind: [] for kind in AuditRead.model_fields},
                               "memories": [mem_public_id(m.id)]}


class TestTheOperatorsAuditLog:
    def test_the_events_with_what_they_read(self, db, tenant, client, auth, admin):
        m = memory(db, tenant)
        client.get(f"/v1/memories/{mem_public_id(m.id)}", headers=auth)
        out = client.get("/admin/audit", headers=admin, params={"user_id": "maria"}).json()
        ev = next(e for e in out["events"] if e["endpoint"] == "memories:get")
        assert ev["read"]["memories"] == [mem_public_id(m.id)] and out["total"] >= 1

    def test_the_ids_read_now_from_the_live_tables(self, db, tenant, client, admin):
        m = memory(db, tenant)
        f = fact(db, tenant, m, "Rome", closed=True)
        erased = mem_public_id(uuid.uuid4())
        other = Tenant(id=uuid.uuid4(), name="Someone else")
        db.add(other)
        db.commit()
        theirs = AgentMemory(tenant_id=other.id, end_user_id="x", content="not yours")
        db.add(theirs)
        db.commit()
        out = client.get("/admin/audit/read", headers=admin, params={
            "memories": ",".join([mem_public_id(m.id), erased, mem_public_id(theirs.id)]),
            "facts": fact_public_id(f.id)}).json()
        assert out["memories"][mem_public_id(m.id)]["text"] == "Maria lives in Milan."
        assert out["memories"][erased] is None, "an erased memory is erased here too"
        assert out["memories"][mem_public_id(theirs.id)] is None, "another account's memory"
        assert out["facts"][fact_public_id(f.id)]["line"] == "Maria lives_in Rome"
        assert out["facts"][fact_public_id(f.id)]["invalid_at"] is not None

    def test_an_api_key_without_admin_cannot_read_it(self, client, auth):
        assert client.get("/admin/audit", headers=auth).status_code in (401, 403)
