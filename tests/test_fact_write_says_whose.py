"""A written fact says whose it is (2026-10-07).

POST /v1/facts answered the write shape without `user_id` and `agent_id`,
which the read shape (GET /v1/facts) has always carried: the Python SDK built
its Fact from that answer and handed back `user_id=None` for a fact written
for an end user (a new-customer test of the hosted product, the same shape
here). Both fields are now on the write shape, additive, as on the read.
"""
import uuid

import pytest

from korely_agent.services import keys as key_service



@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient
    from korely_agent import config
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    config.get_settings.cache_clear()
    try:
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()
    finally:
        config.get_settings.cache_clear()


@pytest.fixture
def head(db, tenant):
    _row, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}",
        scopes=["memories:read", "memories:write"])
    return {"Authorization": f"Bearer {full}"}


def test_the_write_answers_the_end_user_and_the_agent(client, head):
    r = client.post("/v1/facts", headers=head, json={
        "subject": "maria", "predicate": "works_at", "object": "Gemelli hospital",
        "user_id": "maria", "agent_id": "support"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["user_id"], body["agent_id"]) == ("maria", "support")
    read = client.get("/v1/facts", headers=head, params={"user_id": "maria"}).json()["facts"]
    assert [(f["id"], f["user_id"], f["agent_id"]) for f in read] == [(body["id"], "maria", "support")]


def test_a_shared_fact_says_none(client, head):
    r = client.post("/v1/facts", headers=head, json={
        "subject": "Acme", "predicate": "on_plan", "object": "Business plan"})
    assert r.status_code == 201, r.text
    assert (r.json()["user_id"], r.json()["agent_id"]) == (None, None)


def test_the_published_shape_carries_them(client):
    spec = client.get("/openapi.json").json()
    props = spec["components"]["schemas"]["FactWriteResponse"]["properties"]
    assert {"user_id", "agent_id"} <= set(props)
