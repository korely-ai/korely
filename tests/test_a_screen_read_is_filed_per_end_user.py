"""A list the dashboard reads is filed in the trail where each row it
returned lives: one row per project and end user, with the ids of theirs it
returned (review of 6 Nov 2026).

GET /admin/memories and GET /admin/facts wrote one row with the end user of
the request: none for the whole list, so a request of access for one person
(GET /v1/audit?user_id=anna) did not find that the screen had shown their
memories, and a list of the whole account went under no project, which no
project's trail lists. /admin/audit/read already filed its read this way;
the three reads now share one rule.
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentFact, AgentMemory, AuditEvent
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service
from korely_agent.services.ids import fact_public_id, mem_public_id
from tests.conftest import seeded_project_id


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


def head(db, tenant, *, admin=False, project_id=None):
    scopes = list(key_service.DEFAULT_SCOPES) + ([key_service.ADMIN_SCOPE] if admin else [])
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name=uuid.uuid4().hex[:8],
                                          scopes=scopes, project_id=project_id)
    return {"Authorization": f"Bearer {full}"}


def memory(db, tenant, end_user, project):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=end_user,
                    project_id=project, content=f"a memory of {end_user}")
    db.add(m)
    db.commit()
    return m


def fact(db, tenant, end_user, project):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=end_user,
                  project_id=project, subject_label=end_user, predicate_raw="is on",
                  predicate_canonical="is_on", object_label="the Basic plan",
                  valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc))
    db.add(f)
    db.commit()
    return f


@pytest.fixture
def world(db, tenant):
    """anna and bruno in the default project, carla in a second one."""
    default = seeded_project_id(db, tenant.id)
    other = project_service.create_project(db, tenant_id=tenant.id, name="Second").id
    return {
        "default": default, "other": other,
        "anna": (memory(db, tenant, "anna", default), fact(db, tenant, "anna", default)),
        "bruno": (memory(db, tenant, "bruno", default), fact(db, tenant, "bruno", default)),
        "carla": (memory(db, tenant, "carla", other), fact(db, tenant, "carla", other)),
    }


def rows(db, tenant, endpoint):
    db.expire_all()
    return [e for e in db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id).all()
            if (e.meta or {}).get("endpoint") == endpoint]


@pytest.mark.parametrize("path,kind,public", [
    ("/admin/memories", "memories", mem_public_id),
    ("/admin/facts", "facts", fact_public_id),
], ids=["memories", "facts"])
def test_the_whole_accounts_list_is_filed_per_project_and_end_user(
        db, tenant, client, world, path, kind, public):
    r = client.get(path, headers=head(db, tenant, admin=True))
    assert r.status_code == 200, r.text
    endpoint = "admin:memories:list" if kind == "memories" else "admin:facts:list"
    which = 0 if kind == "memories" else 1
    filed = {(e.project_id, e.end_user_id): (e.read_ids or {}).get(kind)
             for e in rows(db, tenant, endpoint)}
    assert filed == {
        (world["default"], "anna"): [public(world["anna"][which].id)],
        (world["default"], "bruno"): [public(world["bruno"][which].id)],
        (world["other"], "carla"): [public(world["carla"][which].id)],
    }
    for e in rows(db, tenant, endpoint):
        assert e.meta[f"{kind}_read"] == 1, "each row counts the rows of its own end user"


def test_a_request_of_access_finds_the_screens_list(db, tenant, client, world):
    assert client.get("/admin/memories", headers=head(db, tenant, admin=True),
                      params={"project_id": str(world["default"])}).status_code == 200
    trail = client.get("/v1/audit", headers=head(db, tenant), params={"user_id": "anna"}).json()
    listed = [e for e in trail["events"] if (e["meta"] or {}).get("endpoint") == "admin:memories:list"]
    assert [e["read"]["memories"] for e in listed] == [[mem_public_id(world["anna"][0].id)]]


def test_a_list_that_found_nothing_is_one_row_with_what_it_asked(db, tenant, client, world):
    r = client.get("/admin/memories", headers=head(db, tenant, admin=True),
                   params={"user_id": "nobody", "project_id": str(world["default"])})
    assert r.status_code == 200 and r.json()["memories"] == []
    (row,) = rows(db, tenant, "admin:memories:list")
    assert (row.end_user_id, row.project_id) == ("nobody", world["default"])
    assert row.meta["memories_read"] == 0
