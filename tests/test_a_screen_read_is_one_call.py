"""The rows a list of the dashboard writes, one per end user it showed, are
one call (review of 14 Nov 2026).

They were written each in a transaction of its own, then settled each in
another, and the Requests page and the Home page counted each as a read:
an admin key's list of 50 end users was 50 reads. Now they go in together,
share `meta.call`, and count once. They are filed by the rows as the page
read them (a second read of the page's ids lost a memory erased in
between), and each says every kind the call read.
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from korely_agent.db.models import AgentFact, AgentMemory, AuditEvent
from korely_agent.services import keys as key_service
from korely_agent.services.ids import fact_public_id, mem_public_id
from tests.conftest import seeded_project_id

PAST = datetime(2026, 3, 1, tzinfo=timezone.utc)


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
def admin(db, tenant):
    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="admin",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    return {"Authorization": f"Bearer {full}"}


def memories(db, tenant, users):
    rows = [dict(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=u,
                 project_id=seeded_project_id(db, tenant.id), content=f"a memory of {u}")
            for u in users]
    db.execute(insert(AgentMemory), rows)
    db.commit()
    return [r["id"] for r in rows]


def fact(db, tenant, user):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=user,
                  project_id=seeded_project_id(db, tenant.id), subject_label=user,
                  predicate_raw="is on", predicate_canonical="is_on", object_label="Basic",
                  valid_from=PAST)
    db.add(f)
    db.commit()
    return f


def trail(db, tenant, endpoint):
    db.expire_all()
    return [e for e in db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id).all()
            if (e.meta or {}).get("endpoint") == endpoint]


def test_one_list_of_fifty_end_users_is_one_read(db, tenant, client, admin):
    from korely_agent.services import usage_report

    memories(db, tenant, [f"v{i:03d}" for i in range(50)])
    assert client.get("/admin/memories", headers=admin, params={"limit": 50}).status_code == 200
    rows = trail(db, tenant, "admin:memories:list")
    assert len(rows) == 50
    # One call: one id of the call on every row, one transaction (its now()).
    assert len({e.meta["call"] for e in rows}) == 1
    assert len({e.ts for e in rows}) == 1
    summary = usage_report.requests_summary(db, tenant.id)
    assert (summary["reads"], summary["total"]) == (1, 1)
    assert usage_report.requests_daily(db, tenant.id)["total_reads"] == 1


def test_a_call_of_one_row_carries_no_call_id(db, tenant, client, admin):
    memories(db, tenant, ["anna"])
    assert client.get("/admin/memories", headers=admin).status_code == 200
    (row,) = trail(db, tenant, "admin:memories:list")
    assert "call" not in row.meta


def test_a_memory_erased_after_the_page_was_read_is_still_in_the_trail(
        db, tenant, client, admin, monkeypatch):
    from korely_agent.services import memory_engine

    ids = memories(db, tenant, ["anna", "bruno"])
    original = memory_engine.AgentMemoryEngine.get_page

    def get_page(self, **kw):
        rows, total = original(self, **kw)
        db.query(AgentMemory).filter(AgentMemory.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        return rows, total

    monkeypatch.setattr(memory_engine.AgentMemoryEngine, "get_page", get_page)
    r = client.get("/admin/memories", headers=admin)
    assert len(r.json()["memories"]) == 2
    filed = {e.end_user_id: e.read_ids["memories"] for e in trail(db, tenant, "admin:memories:list")}
    assert filed == {"anna": [mem_public_id(ids[0])], "bruno": [mem_public_id(ids[1])]}


def test_every_row_says_each_kind_the_call_read(db, tenant, client, admin):
    (mid,) = memories(db, tenant, ["anna"])
    fid = fact(db, tenant, "bruno").id
    r = client.get("/admin/audit/read", headers=admin,
                   params={"memories": mem_public_id(mid), "facts": fact_public_id(fid)})
    assert r.status_code == 200
    rows = {e.end_user_id: e for e in trail(db, tenant, "admin:audit:read")}
    assert rows["anna"].read_ids == {"memories": [mem_public_id(mid)], "facts": []}
    assert rows["bruno"].read_ids == {"memories": [], "facts": [fact_public_id(fid)]}
    assert (rows["bruno"].meta["memories_read"], rows["anna"].meta["facts_read"]) == (0, 0)
    # A list of facts read no memories: none on its row.
    assert client.get("/admin/facts", headers=admin).status_code == 200
    (listed,) = trail(db, tenant, "admin:facts:list")
    assert listed.read_ids == {"facts": [fact_public_id(fid)]}
    assert "memories_read" not in listed.meta


def test_the_requests_list_shows_one_call_as_one_row(db, tenant, client, admin):
    """The list of the Requests page counts as its summary does: two lists
    of 50 end users are two requests, not 100 rows (review of 14 Nov
    2026), each saying how many end users it touched."""
    memories(db, tenant, [f"v{i:03d}" for i in range(50)])
    for _ in range(2):
        assert client.get("/admin/memories", headers=admin, params={"limit": 50}).status_code == 200
    assert len(trail(db, tenant, "admin:memories:list")) == 100
    listed = client.get("/admin/requests", headers=admin).json()
    summary = client.get("/admin/requests/summary", headers=admin).json()
    assert listed["total"] == summary["total"] == 2
    assert [(r["endpoint"], r["end_users"], r["user_id"]) for r in listed["requests"]] == [
        ("admin:memories:list", 50, None)] * 2


def test_a_call_of_one_end_user_is_listed_with_them(db, tenant, client, admin):
    memories(db, tenant, ["anna"])
    assert client.get("/admin/memories", headers=admin).status_code == 200
    assert client.get("/admin/memories", headers=admin, params={"user_id": "nobody"}).status_code == 200
    rows = client.get("/admin/requests", headers=admin).json()["requests"]
    # Newest first: the read of nobody's memories found none and is filed
    # with the end user it asked for.
    assert [(r["end_users"], r["user_id"]) for r in rows] == [(1, "nobody"), (1, "anna")]
    assert client.get("/admin/requests", headers=admin, params={"limit": 1}).json()["total"] == 1


def test_the_requests_list_reads_the_summarys_window(db, tenant, client, admin):
    """The list groups the calls in the database, over the window the
    summary beside it counts (`days`, 30 by default): a call older than it is
    in neither, and the rows grouped are bounded by the trail's index."""
    from datetime import datetime, timedelta, timezone

    from korely_agent.services import audit

    audit.record(db, tenant.id, audit.READ, actor="rest", meta={"endpoint": "context:get"})
    db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id).update(
        {AuditEvent.ts: datetime.now(timezone.utc) - timedelta(days=40)})
    db.commit()
    audit.record(db, tenant.id, audit.READ, actor="rest", meta={"endpoint": "search:post"})
    listed = client.get("/admin/requests", headers=admin).json()
    assert [r["endpoint"] for r in listed["requests"]] == ["search:post"]
    assert listed["total"] == client.get("/admin/requests/summary", headers=admin).json()["total"] == 1
    wider = client.get("/admin/requests", headers=admin, params={"days": 60}).json()
    assert [r["endpoint"] for r in wider["requests"]] == ["search:post", "context:get"]
