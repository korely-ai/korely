"""An account's API keys are capped and listed a page at a time (audit of 27
Oct 2026): GET /admin/keys read every key
of the account, and an account could make as many as it liked."""
import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentApiKey
from korely_agent.services import keys as key_service


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


def _fill(db, tenant, n):
    """n more keys, straight into the table (the cap is on the door)."""
    for i in range(n):
        key_service.create_api_key(db, tenant_id=tenant.id, name=f"bulk-{i}")


def test_a_ceiling_of_keys_and_room_again_after_a_revoke(db, tenant, client, admin, monkeypatch):
    monkeypatch.setattr(key_service, "MAX_API_KEYS", 4)
    _fill(db, tenant, 3)                       # the admin key and three: four
    r = client.post("/admin/keys", headers=admin, json={"name": "one-too-many"})
    assert r.status_code == 409 and r.json()["code"] == "too_many_keys"
    first = db.query(AgentApiKey).filter(AgentApiKey.name == "bulk-0").one()
    assert client.post(f"/admin/keys/{first.id}/revoke", headers=admin).status_code in (200, 204)
    assert client.post("/admin/keys", headers=admin, json={"name": "fits-now"}).status_code == 201


def test_a_connected_apps_key_is_not_counted(db, tenant, monkeypatch):
    monkeypatch.setattr(key_service, "MAX_API_KEYS", 1)
    from korely_agent.db.models import OAuthClient

    db.add(OAuthClient(client_id="c1", info={}))
    db.commit()
    key_service.create_api_key(db, tenant_id=tenant.id, name="mine")
    key_service.create_api_key(db, tenant_id=tenant.id, name="app", oauth_client_id="c1")
    with pytest.raises(key_service.TooManyKeys):
        key_service.create_api_key(db, tenant_id=tenant.id, name="another")


def test_the_list_is_a_page_with_the_live_keys_first(db, tenant, client, admin):
    _fill(db, tenant, 5)
    old = db.query(AgentApiKey).filter(AgentApiKey.name == "bulk-0").one()
    client.post(f"/admin/keys/{old.id}/revoke", headers=admin)
    names = [k["name"] for k in client.get("/admin/keys", headers=admin).json()]
    assert names[-1] == "bulk-0" and len(names) == 6
    page = client.get("/admin/keys?limit=2&offset=1", headers=admin).json()
    assert [k["name"] for k in page] == names[1:3]
    assert client.get("/admin/keys?limit=0", headers=admin).status_code == 422
