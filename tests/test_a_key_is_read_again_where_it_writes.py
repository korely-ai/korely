"""A key revoked while its request waits does not write (an install of one's own).

POST /admin/webhooks and its enable look the webhook's address up before
they write (up to threads.LOOKUP_SECONDS); POST /admin/keys makes the key
after its other checks. Up to 1.0.9 they wrote with the key as it was when
the request came in. Now each reads it again in the transaction that writes
(core/auth.still_admin, CHANGELOG 1.0.10): revoked meanwhile, 401
`invalid_key`; a dashboard session is not read again. The Cloud's case, an
adoption that takes `admin` meanwhile, is tests/test_hosted_adoption_races.py."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentApiKey, AgentWebhookEndpoint
from korely_agent.services import keys as key_service
from korely_agent.services import webhooks as webhook_service


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
def admin_key(db, tenant):
    row, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="admin",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    return row.id, {"Authorization": f"Bearer {full}"}


def _revoke_in_own_session(key_id, tenant_id):
    from korely_agent.db import get_sessionmaker

    s = get_sessionmaker()()
    try:
        assert key_service.revoke_api_key(s, key_id=key_id, tenant_id=tenant_id)
    finally:
        s.close()


def test_a_webhook_is_not_made_by_a_key_revoked_during_the_lookup(client, db, tenant, admin_key,
                                                                    monkeypatch):
    key_id, headers = admin_key

    def slow_lookup(url):
        _revoke_in_own_session(key_id, tenant.id)
        return url

    monkeypatch.setattr(webhook_service, "check_url", slow_lookup)
    r = client.post("/admin/webhooks", json={"url": "https://hooks.example/h"}, headers=headers)
    assert r.status_code == 401 and r.json()["code"] == "invalid_key", r.text
    assert key_service.NOT_LIVE in r.json()["message"]
    db.expire_all()
    assert db.query(AgentWebhookEndpoint).filter(AgentWebhookEndpoint.tenant_id == tenant.id).count() == 0


def test_a_webhook_is_not_switched_on_by_a_key_revoked_during_the_lookup(client, db, tenant,
                                                                         admin_key, monkeypatch):
    key_id, headers = admin_key
    ep = AgentWebhookEndpoint(tenant_id=tenant.id, url="https://hooks.example/h",
                              secret="whsec_x", events=["*"], active=False)
    db.add(ep)
    db.commit()

    def slow_lookup(url):
        _revoke_in_own_session(key_id, tenant.id)
        return url

    monkeypatch.setattr(webhook_service, "check_url", slow_lookup)
    r = client.post(f"/admin/webhooks/{ep.id}/enable", headers=headers)
    assert r.status_code == 401 and r.json()["code"] == "invalid_key", r.text
    db.expire_all()
    assert db.get(AgentWebhookEndpoint, ep.id).active is False


def test_a_key_is_not_made_by_a_key_revoked_meanwhile(client, db, tenant, admin_key, monkeypatch):
    key_id, headers = admin_key
    made = key_service.create_api_key

    def revoked_first(db_, **kw):
        _revoke_in_own_session(key_id, tenant.id)
        return made(db_, **kw)

    monkeypatch.setattr(key_service, "create_api_key", revoked_first)
    r = client.post("/admin/keys", json={"name": "late"}, headers=headers)
    assert r.status_code == 401 and r.json()["code"] == "invalid_key", r.text
    db.expire_all()
    assert db.query(AgentApiKey).filter(AgentApiKey.name == "late").first() is None


def test_a_live_admin_key_writes_as_before(client, db, tenant, admin_key, monkeypatch):
    _key_id, headers = admin_key
    monkeypatch.setattr(webhook_service, "check_url", lambda u: u)
    r = client.post("/admin/webhooks", json={"url": "https://hooks.example/h"}, headers=headers)
    assert r.status_code == 201, r.text
    assert client.post("/admin/keys", json={"name": "next"}, headers=headers).status_code == 201
