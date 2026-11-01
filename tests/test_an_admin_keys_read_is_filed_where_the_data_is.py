"""A dashboard read made with an admin-scope key is filed where the data it
read lives, as an operator's is (audit of 20 Oct 2026).

services.usage.log_v1_request computed the project for a principal and then
filed every row with a key under the key's own project: an admin key of P
reading Q's end user wrote rows in P, which P's read keys listed (Q's end
user and the ids read) with GET /v1/audit, and Q's erasure, which relabels
Q's rows and the rows of no project, left them with the id in clear.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentMemory, AuditEvent
from korely_agent.services import keys as key_service
from korely_agent.services import projects
from korely_agent.services.ids import mem_public_id

ADMIN = list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE]
WHO = "collaudo+r4@korely.ai"


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


def _key(db, tenant, **kw):
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id,
                                          name=f"k-{uuid.uuid4().hex[:6]}", **kw)
    return {"Authorization": f"Bearer {full}"}


def test_an_admin_key_of_p_reading_q_files_nothing_in_p(db, tenant, client):
    p = projects.create_project(db, tenant_id=tenant.id, name="P")
    q = projects.create_project(db, tenant_id=tenant.id, name="Q")
    m = AgentMemory(tenant_id=tenant.id, project_id=q.id, end_user_id=WHO, content="dummy")
    db.add(m)
    db.commit()
    admin_p = _key(db, tenant, project_id=p.id, scopes=ADMIN)
    reader_p = _key(db, tenant, project_id=p.id, scopes=["memories:read"])

    assert client.get("/admin/memories", params={"user_id": WHO}, headers=admin_p).status_code == 200
    assert client.get(f"/admin/memories/{mem_public_id(m.id)}", headers=admin_p).status_code == 200
    assert client.get("/admin/facts", params={"project_id": str(q.id), "user_id": WHO},
                      headers=admin_p).status_code == 200

    db.expire_all()
    rows = db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id,
                                       AuditEvent.end_user_id == WHO).all()
    assert len(rows) == 3
    assert p.id not in {r.project_id for r in rows}, [(r.meta, r.project_id) for r in rows]
    # The memory's own read is filed in its project.
    assert {r.project_id for r in rows if (r.meta or {}).get("endpoint") == "admin:memories:get"} \
        == {q.id}
    seen = client.get("/v1/audit", headers=reader_p).json()["events"]
    assert WHO not in {e.get("user_id") for e in seen}, seen
