"""What an id with a Unicode line break (U+2028 LINE SEPARATOR, U+2029
PARAGRAPH SEPARATOR) meets on each door from 1.0.15, the release that
refuses them where it refused the control characters (services/ids.py,
`namespace_id_problem`), for whoever upgrades with such ids in use: the
writes and a key's end users refuse a new one; what is stored is read and
erased as before; a key that already lists one keeps working; a batch job
queued before the upgrade fails that row alone. The OAuth consent and its
code are in tests/test_oauth.py.
"""
import uuid
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentMemory
from korely_agent.services import batch as batch_service
from korely_agent.services import keys as key_service
from korely_agent.services import operators as operator_service
from korely_agent.services import projects as project_service

BREAKS = ["a\u2028b", "a\u2029b"]


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
def default(db, tenant):
    return project_service.get_or_create_default_project(db, tenant.id)


def _head(raw):
    return {"Authorization": f"Bearer {raw}"}


@pytest.mark.parametrize("lb", BREAKS, ids=["line", "paragraph"])
def test_a_key_cannot_list_such_an_end_user(client, db, tenant, lb):
    op, _pw = operator_service.create(db, tenant_id=tenant.id, username=f"op-{uuid.uuid4().hex[:6]}")
    _row, raw = operator_service.open_session(db, op)
    r = client.post("/admin/keys", headers=_head(raw),
                    json={"name": "k", "scopes": ["memories:read"], "users": [lb, "founder"]})
    assert r.status_code == 422 and "line break" in r.json()["detail"], r.text
    with pytest.raises(key_service.BadEndUsers):
        key_service.create_api_key(db, tenant_id=tenant.id, name="k2", end_users=[lb])


@pytest.mark.parametrize("lb", BREAKS, ids=["line", "paragraph"])
def test_what_is_stored_is_read_and_erased_and_a_new_write_refused(client, db, tenant, default, lb):
    _row, raw = key_service.create_api_key(db, tenant_id=tenant.id, name="whole")
    db.add(AgentMemory(tenant_id=tenant.id, project_id=default.id, end_user_id=lb,
                       content="hello", graph_sync_status="done"))
    db.commit()
    r = client.get("/v1/memories", params={"user_id": lb}, headers=_head(raw))
    assert r.status_code == 200 and r.json()["memories"], r.text
    r = client.post("/v1/memories", json={"content": "x", "user_id": lb}, headers=_head(raw))
    assert r.status_code == 422 and r.json()["detail"][0]["type"] == "invalid_namespace_id", r.text
    r = client.delete(f"/v1/users/{quote(lb, safe='')}/memories", headers=_head(raw))
    assert r.status_code == 200 and r.json()["memories_deleted"] == 1, r.text


def test_a_key_that_already_lists_one_keeps_working(client, db, tenant, default):
    row, raw = key_service.create_api_key(db, tenant_id=tenant.id, name="old",
                                          end_users=["founder"])
    row.end_users = [BREAKS[0], "founder"]          # as 1.0.13 could store it
    db.commit()
    for user in (BREAKS[0], "founder"):
        r = client.get("/v1/memories", params={"user_id": user}, headers=_head(raw))
        assert r.status_code == 200, (user, r.text)


def test_a_batch_queued_before_the_upgrade_fails_that_row_alone(db, tenant, default):
    """A job POST /v1/batch accepted before 1.0.15 (the door refuses such a
    row now): the worker checks each row again by the rules of every write
    (services/batch.py, `_import_one`), so that row fails with its reason
    and the others are imported."""
    job = batch_service.enqueue_batch(db, tenant.id, [{"content": "one", "user_id": BREAKS[1]},
                                                      {"content": "two", "user_id": "ok"}],
                                      project_id=default.id)
    batch_service.process_batch_jobs(db)
    db.expire_all()
    done = batch_service.job_status(db, tenant.id, job.id)
    assert (done["status"], done["imported"], done["failed"]) == ("completed", 1, 1), done
    (error,) = done["errors"]
    assert error["index"] == 0 and error["error"].startswith("user_id cannot contain a line break")
