"""An account may declare only so many names (review of the audit of 27 Oct
2026): a key that declared one per end user made every name read of the account
heavier."""
import pytest
from fastapi.testclient import TestClient

from korely_agent.services import keys as key_service
from korely_agent.services.facts import entities


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


def _head(db, tenant, admin=False):
    scopes = list(key_service.DEFAULT_SCOPES) + ([key_service.ADMIN_SCOPE] if admin else [])
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name="admin" if admin else "agent",
                                          scopes=scopes)
    return {"Authorization": f"Bearer {full}"}


def test_a_ceiling_of_declared_names_on_both_doors(db, tenant, client, monkeypatch):
    monkeypatch.setattr(entities, "MAX_DECLARED_NAMES", 2)
    agent, admin = _head(db, tenant), _head(db, tenant, admin=True)
    assert client.post("/v1/users/ann/names", headers=agent, json={"name": "Annie"}).status_code == 201
    assert client.post("/v1/users/bob/names", headers=agent, json={"name": "Bobby"}).status_code == 201
    over = client.post("/v1/users/cy/names", headers=agent, json={"name": "Cyrus"})
    assert over.status_code == 409 and over.json()["code"] == "too_many_names"
    # The same name declared again answers as before, past the ceiling.
    assert client.post("/v1/users/ann/names", headers=agent, json={"name": "Annie"}).status_code == 201
    over = client.post("/admin/aliases", headers=admin,
                       json={"alias": "Acme", "canonical": "Globex", "user_id": "ann"})
    assert over.status_code == 409 and over.json()["code"] == "too_many_names"
    # Room again after one is deleted.
    rows = client.get("/admin/aliases", headers=admin).json()["aliases"]
    assert client.delete(f"/admin/aliases/{rows[0]['id']}", headers=admin).status_code == 200
    assert client.post("/v1/users/cy/names", headers=agent, json={"name": "Cyrus"}).status_code == 201
