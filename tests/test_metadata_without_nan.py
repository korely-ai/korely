"""NaN and Infinity in a write's metadata are a 422, never a 500 (2026-10-06).

Python's JSON parser reads `NaN` and `Infinity`; PostgreSQL refuses them
("invalid input syntax for type json"), so POST /v1/memories with
{"score": NaN} died inside its transaction with a 500, and a batch item with
one would have failed in the worker, minutes later. Found by the hosted
product (e99d809); the rule is the one module every door reads
(services/limits.py), so the single write and every item of a batch say the
same thing, naming where the number is.
"""
import pytest
from fastapi.testclient import TestClient

from korely_agent.services import keys as key_service
from korely_agent.services import limits



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
def auth(db, tenant):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="t")
    return {"Authorization": f"Bearer {full}", "Content-Type": "application/json"}


def test_the_rule_names_where_the_number_is():
    assert "'limits.max'" in limits.metadata_problem({"limits": {"max": float("nan")}})
    assert "'scores[1]'" in limits.metadata_problem({"scores": [1, float("inf")]})
    assert limits.metadata_problem({"score": 0.5, "n": 3, "ok": True, "none": None}) is None


@pytest.mark.parametrize("raw, where", [
    ('{"limits": {"max": NaN}}', "limits.max"),
    ('{"scores": [1, Infinity]}', "scores[1]"),
    ('{"t": -Infinity}', "t"),
])
def test_a_write_is_refused_naming_the_path(client, auth, raw, where):
    r = client.post("/v1/memories", headers=auth,
                    content='{"content": "Maria lives in Rome.", "user_id": "maria", "metadata": %s}' % raw)
    assert r.status_code == 422, r.text
    assert where in r.text


def test_every_item_of_a_batch_too(client, auth):
    r = client.post("/v1/batch", headers=auth,
                    content='{"memories": [{"content": "fine", "metadata": {"a": 1}},'
                            ' {"content": "Maria lives in Rome.", "metadata": {"score": NaN}}]}')
    assert r.status_code == 422, r.text
    assert "memories[1]" in r.text and "score" in r.text
