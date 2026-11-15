"""Only the truth as it stands is corrected (2026-10-07).

PATCH /v1/facts/{id} on a fact that was already history (superseded,
forgotten or ended) wrote the corrected value as a new fact, and the
contradiction pass on it closed the fact that holds now: correcting the past
changed the present. Found by a new-customer test of the hosted
product, the same code here. Now such a correction is refused before anything
is written, with the hosted product's 409 `fact_not_current`, whose message
names the current fact to correct instead, or says to write the value that
holds now; the dashboard's correction answers the same 409.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.db.models import AgentFact
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


def write(client, head, obj):
    r = client.post("/v1/facts", headers=head, json={
        "subject": "maria", "predicate": "on_plan", "object": obj, "user_id": "maria"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def correct(client, head, fid, obj):
    return client.patch(f"/v1/facts/{fid}", headers=head, json={"object": obj})


def open_objects(db):
    db.expire_all()
    now = datetime.now(timezone.utc)
    return sorted(f.object_label for f in db.query(AgentFact).all()
                  if f.invalid_at is None or f.invalid_at > now)


def test_a_superseded_fact_names_the_one_to_correct(client, head, db):
    old = write(client, head, "Pro plan")
    r = correct(client, head, old, "Business plan")
    assert r.status_code == 200, r.text
    current = r.json()["id"]
    before = db.query(AgentFact).count()

    r = correct(client, head, old, "Team plan")

    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "fact_not_current"
    assert current in body["message"]
    # The same id as a field, for a client that retries on it (7 Oct 2026,
    # the hosted product's name).
    assert body["current_fact_id"] == body["detail"]["current_fact_id"] == current
    assert db.query(AgentFact).count() == before, "nothing written"
    assert open_objects(db) == ["Business plan"], "the fact that holds now stays"


def test_the_chain_is_followed_to_the_fact_true_now(client, head):
    first = write(client, head, "Pro plan")
    second = correct(client, head, first, "Business plan").json()["id"]
    third = correct(client, head, second, "Team plan").json()["id"]
    r = correct(client, head, first, "Free plan")
    assert r.status_code == 409 and third in r.json()["message"]
    assert r.json()["current_fact_id"] == third


def test_a_forgotten_fact_says_to_write_the_value_of_now(client, head, db):
    fid = write(client, head, "Pro plan")
    assert client.post(f"/v1/facts/{fid}/forget", headers=head, json={}).status_code == 200
    r = correct(client, head, fid, "Team plan")
    assert r.status_code == 409
    assert "POST /v1/facts" in r.json()["message"]
    assert "current_fact_id" in r.json() and r.json()["current_fact_id"] is None
    assert open_objects(db) == []


def test_a_fact_that_ends_later_is_still_current(client, head, db):
    fid = write(client, head, "Pro plan")
    row = db.query(AgentFact).one()
    row.invalid_at = datetime.now(timezone.utc) + timedelta(days=10)
    db.commit()
    assert correct(client, head, fid, "Business plan").status_code == 200


def test_the_current_fact_is_corrected_as_before(client, head, db):
    fid = write(client, head, "Pro plan")
    r = correct(client, head, fid, "Business plan")
    assert r.status_code == 200
    assert fid in r.json()["invalidated"]
    assert open_objects(db) == ["Business plan"]


def test_the_dashboard_correction_answers_the_same(client, db, tenant):
    _row, plain = key_service.create_api_key(
        db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}",
        scopes=["memories:read", "memories:write", "admin"])
    admin = {"Authorization": f"Bearer {plain}"}
    old = write(client, admin, "Pro plan")
    current = correct(client, admin, old, "Business plan").json()["id"]
    r = client.patch(f"/admin/facts/{old}", headers=admin, json={"object": "Team plan"})
    assert r.status_code == 409, r.text
    # The same refusal as /v1's, field for field (audit of 17 Oct 2026: it
    # was a sentence with the code `conflict`).
    body = r.json()
    assert body["code"] == body["detail"]["code"] == "fact_not_current"
    assert body["current_fact_id"] == current and current in body["message"]
    assert open_objects(db) == ["Business plan"]


def test_a_fact_dated_ahead_is_corrected_where_it_begins(client, head, db):
    """A contract that starts next month, corrected today: the old row was
    closed now, before it began, and the new value was true from today
    (audit of 17 Oct 2026). Now the old row closes where it was to begin and
    the corrected value begins there."""
    ahead = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0)
    r = client.post("/v1/facts", headers=head, json={
        "subject": "maria", "predicate": "uses", "object": "Jira", "user_id": "maria",
        "valid_from": ahead.isoformat()})
    assert r.status_code == 201, r.text
    r = correct(client, head, r.json()["id"], "Linear")
    assert r.status_code == 200, r.text
    db.expire_all()
    rows = {f.object_label: f for f in db.query(AgentFact).all()}
    old, new = rows["Jira"], rows["Linear"]
    assert old.invalid_at >= old.valid_from, "closed before it began"
    assert new.valid_from == old.valid_from == ahead
    assert client.get("/v1/facts", headers=head, params={"user_id": "maria"}).json()["facts"] == []
