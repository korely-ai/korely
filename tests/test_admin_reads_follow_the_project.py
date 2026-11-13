"""Every read of the dashboard's data pages takes the project chosen at the
top of the screen (6 Nov 2026).

With a project of 9 facts chosen in the switcher, the Memory Graph drew the
2,808 facts of the whole account, after a reload too: the page called
/admin/facts without `project_id`, and /admin/memories,
/admin/memories/categories, /admin/namespaces, /admin/requests (and its
summary and daily series) and /admin/audit did not take one at all. Without
it a dashboard session reads every project of the account.

Each of them now takes `project_id` as /admin/facts and /admin/memories/search
do (api/admin._within_project): one project of the account, read as its keys
read it; left out, every project; 404 for a project that is not the
account's; an empty one is a 422, never the whole account. One account, two
projects with the same agent and the same family, the same assertion for
every route: a read of project A shows nothing of B, its counts included.
"""
import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentFact, AgentMemory, AuditEvent, Tenant
from korely_agent.services import audit
from korely_agent.services import keys as key_service
from korely_agent.services import projects
from korely_agent.services.ids import mem_public_id

ADMIN = list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE]


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


def _admin(db, tenant_id):
    _r, full = key_service.create_api_key(db, tenant_id=tenant_id,
                                          name=f"admin-{uuid.uuid4().hex[:6]}", scopes=ADMIN)
    return {"Authorization": f"Bearer {full}"}


# One agent and one family in both projects, so that a count of a second
# level (an agent's facts, a family's memories) that leaks across the wall
# shows as a wrong number, not only as a name of B (review of 6 Nov 2026).
AGENT, FAMILY = "support-bot", "work"


def _fill(db, tenant_id, project, tag):
    """One memory of the project, carrying `tag`, the fact it stated, a write
    and a read in the trail."""
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant_id, project_id=project.id,
                    end_user_id=f"user-{tag}", agent_id=AGENT, content=f"memory-{tag}")
    db.add(m)
    db.flush()
    db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant_id, project_id=project.id,
                     end_user_id=f"user-{tag}", agent_id=AGENT,
                     subject_label=f"subject-{tag}", predicate_raw="likes",
                     predicate_canonical="likes", predicate_family=FAMILY,
                     object_label=f"object-{tag}", source_memory_id=m.id,
                     valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    db.commit()
    for action in (audit.WRITE, audit.READ):
        audit.record(db, tenant_id, action, actor="rest", end_user_id=f"user-{tag}",
                     meta={"endpoint": f"test:{tag}"}, project_id=project.id)
    return m


@pytest.fixture
def two(db, tenant):
    """A holds one memory and one fact, B two of each."""
    a = projects.create_project(db, tenant_id=tenant.id, name="A")
    b = projects.create_project(db, tenant_id=tenant.id, name="B")
    ma = _fill(db, tenant.id, a, "a")
    mb = _fill(db, tenant.id, b, "b")
    _fill(db, tenant.id, b, "c")
    return a, b, ma, mb


# Every data read of the screen, and the params it is read with.
READS = [
    ("/admin/memories", {}),
    ("/admin/memories/count", {}),
    ("/admin/memories/categories", {}),
    ("/admin/facts", {"include_invalidated": True}),
    ("/admin/namespaces", {}),
    ("/admin/requests", {}),
    ("/admin/requests/summary", {}),
    ("/admin/requests/daily", {}),
    ("/admin/audit", {}),
]

# What of project B a read of the whole account shows, by route: a tag, or,
# for the reads that answer numbers, the number read from the answer, which
# is A's 1, B's 2 and the account's 3.
SEEN_OF_B = {
    "/admin/memories": "memory-b",
    "/admin/facts": "object-b",
    "/admin/requests": "test:b",
    "/admin/requests/summary": "test:b",
    "/admin/audit": "user-b",
}
COUNTED = {
    "/admin/memories/count": lambda d: d["total"],
    "/admin/memories/categories": lambda d: {c["family"]: c["count"] for c in d}[FAMILY],
    "/admin/namespaces": lambda d: (d["agents"][0]["memories"], d["agents"][0]["facts"],
                                    d["agents"][0]["end_users"]),
    "/admin/requests/daily": lambda d: d["total_writes"],
}
EXPECTED = {
    "/admin/memories/count": (1, 2, 3),
    "/admin/memories/categories": (1, 2, 3),
    "/admin/namespaces": ((1, 1, 1), (2, 2, 2), (3, 3, 3)),
    "/admin/requests/daily": (1, 2, 3),
}


@pytest.mark.parametrize("path,params", READS, ids=[p for p, _ in READS])
def test_a_read_of_project_a_shows_nothing_of_b(db, tenant, client, two, path, params):
    a, b, _ma, _mb = two
    mine = _admin(db, tenant.id)
    every = client.get(path, params=params, headers=mine)
    only_a = client.get(path, params={**params, "project_id": str(a.id)}, headers=mine)
    only_b = client.get(path, params={**params, "project_id": str(b.id)}, headers=mine)
    assert every.status_code == only_a.status_code == only_b.status_code == 200, \
        (every.text, only_a.text)
    if path in COUNTED:
        read = COUNTED[path]
        assert (read(only_a.json()), read(only_b.json()), read(every.json())) == EXPECTED[path]
        return
    marker = SEEN_OF_B[path]
    assert marker in json.dumps(every.json()), "the whole account shows B"
    assert marker not in json.dumps(only_a.json()), f"{path} with project A shows B"
    assert marker in json.dumps(only_b.json())


@pytest.mark.parametrize("path,params", READS, ids=[p for p, _ in READS])
def test_a_project_that_is_not_the_accounts_is_a_404(db, tenant, client, path, params):
    mine = _admin(db, tenant.id)
    other = Tenant(id=uuid.uuid4(), name="Somebody Else")
    db.add(other)
    db.commit()
    theirs = projects.create_project(db, tenant_id=other.id, name="theirs")
    for asked in (str(theirs.id), str(uuid.uuid4()), "not-an-id"):
        r = client.get(path, params={**params, "project_id": asked}, headers=mine)
        assert r.status_code == 404, (path, asked, r.status_code)


# The search too: refused before it waits for the embedder.
EMPTY_ASKED = READS + [("/admin/memories/search", {"query": "plan"})]


@pytest.mark.parametrize("path,params", EMPTY_ASKED, ids=[p for p, _ in EMPTY_ASKED])
def test_an_empty_project_is_refused_never_the_whole_account(db, tenant, client, two, path,
                                                              params):
    """`project_id=` read as no project at all, the whole account: refused,
    as an empty `user_id` is (review of 6 Nov 2026)."""
    r = client.get(path, params={**params, "project_id": ""}, headers=_admin(db, tenant.id))
    assert r.status_code == 422, (path, r.status_code)


def test_the_counts_of_one_project_are_its_own(db, tenant, client, two):
    """The numbers the Home page and Agents show are the project's: the one
    agent with its one end user and one fact; one write in the summary."""
    a, _b, _ma, _mb = two
    mine = _admin(db, tenant.id)
    agents = client.get("/admin/namespaces", params={"project_id": str(a.id)},
                        headers=mine).json()
    assert agents["total"] == 1
    assert agents["agents"] == [{"agent_id": AGENT, "memories": 1, "end_users": 1, "facts": 1}]
    listed = client.get("/admin/memories", params={"project_id": str(a.id)}, headers=mine).json()
    assert listed["total"] == 1
    summary = client.get("/admin/requests/summary", params={"project_id": str(a.id)},
                         headers=mine).json()
    assert summary["writes"] == 1
    assert {e["endpoint"] for e in summary["by_endpoint"]} >= {"test:a"}


def test_the_home_pages_count_is_no_read_of_a_memory(db, tenant, client, two):
    """The Home page's "Stored memories" is a number: it read one memory's
    id with GET /admin/memories?limit=1 and put a read in the project's
    trail at every visit (review of 6 Nov 2026). /admin/memories/count reads
    none and files nothing, as the categories and the request counts."""
    a, _b, _ma, _mb = two
    mine = _admin(db, tenant.id)
    before = db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id).count()
    assert client.get("/admin/memories/count", params={"project_id": str(a.id)},
                      headers=mine).json() == {"total": 1}
    db.expire_all()
    assert db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id).count() == before


def test_a_projects_read_is_filed_in_that_project(db, tenant, client, two):
    """The list's read goes in the trail under the project it read, where
    that project's keys read the trail (services/usage.log_v1_request); the
    whole account's under no project, as before."""
    a, _b, ma, _mb = two
    mine = _admin(db, tenant.id)
    client.get("/admin/memories", params={"project_id": str(a.id)}, headers=mine)
    client.get("/admin/memories", headers=mine)
    db.expire_all()
    rows = (db.query(AuditEvent)
              .filter(AuditEvent.tenant_id == tenant.id,
                      AuditEvent.meta["endpoint"].as_string() == "admin:memories:list")
              .order_by(AuditEvent.ts, AuditEvent.id).all())
    assert [r.project_id for r in rows] == [a.id, None]
    assert mem_public_id(ma.id) in (rows[0].read_ids or {}).get("memories", [])
