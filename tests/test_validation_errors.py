"""A validation error does not read back aloud what you sent it.

Pydantic v2 puts the offending value in `input`, and FastAPI's default handler
lets it through, so a 422 returns the body that caused it. Almost always that
is harmless and handy. On PUT /admin/provider it is the client's model key:
get the field name wrong and the key comes back.

Found by pasting a real key into a real install, sending `api_key` where the
endpoint wants `key`. The caller already knows what they sent, so no stranger
learns anything: the point is where the secret ends up afterwards, in the
client's log, in the terminal, in the bug report pasted into a chat.
"""
import pytest
from fastapi.testclient import TestClient

from korely_agent.services import keys as key_service
from korely_agent.services import operators as operator_service



@pytest.fixture
def client(db, monkeypatch):
    # Imported here and not at the top: `korely_agent.main` builds the app at
    # module level for uvicorn, that needs settings, and settings need a
    # database. A top-level import makes this file fail to collect without a
    # DATABASE_URL, and a collection error takes the whole suite with it, not
    # just this file. The rest of the suite already does it this way.
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c


@pytest.fixture
def operator(db, tenant):
    _row, password = operator_service.create(
        db, tenant_id=tenant.id, username="op")
    return {"username": "op", "password": password}


class TestThe422DoesNotRepeatTheBody:
    def test_the_model_key_does_not_come_back(self, client, db, tenant, operator):
        sess = client.post("/admin/session", json=operator)
        assert sess.status_code == 200
        token = sess.json()["token"]

        secret = "AQ.Ab8RN6-this-must-not-come-back"
        r = client.put("/admin/provider",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"api_key": secret})

        assert r.status_code == 422
        assert secret not in r.text

    def test_but_it_still_says_which_field_is_wrong(self, client, db, tenant, operator):
        """Removing `input` must not make the message useless: `loc` and `msg`
        are the part that helps, and that is the part that stays."""
        sess = client.post("/admin/session", json=operator)
        token = sess.json()["token"]
        r = client.put("/admin/provider",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"api_key": "x"})
        body = r.json()
        assert r.status_code == 422
        assert body["detail"][0]["loc"] == ["body", "key"]
        assert "required" in body["detail"][0]["msg"].lower()

    def test_it_holds_for_every_endpoint_not_just_that_one(self, client, db, tenant):
        """The handler is global on purpose. An endpoint that carries no
        secrets today can carry them tomorrow, and the rule "remember not to
        repeat the body" is exactly the kind of rule somebody forgets."""
        _row, key = key_service.create_api_key(db, tenant_id=tenant.id, name="k")
        r = client.post("/v1/memories",
                        headers={"Authorization": f"Bearer {key}"},
                        json={"not_a_field": "AVALUETORECOGNISE"})
        assert r.status_code == 422
        assert "AVALUETORECOGNISE" not in r.text
