"""GET /v1/context refuses a parameter it does not take (review of 31 Oct
2026).

`as_of` is a parameter of GET /v1/facts and GET /v1/profile, and the
context took it in silence: no error, and a block of the facts true today
for a caller who had asked about March. The block is built with today's
memories and has no "true on a date"; `known_as_of` rebuilds it as the
store knew it then. A name the route does not take is now a 422 that says
which names it takes, as the MCP's korely_get_context, both SDKs and every
request body already refuse one.
"""
import pytest
from fastapi.testclient import TestClient

from korely_agent.services import context as ctx
from korely_agent.services import keys as key_service


@pytest.fixture
def call(db, tenant, monkeypatch):
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: None)
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="t")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        def get(headers=None, **params):
            return c.get("/v1/context", params={"query": "what plan is Anna on", **params},
                         headers={"Authorization": f"Bearer {full}"} if headers is None else headers)
        yield get
    app.dependency_overrides.clear()


def test_as_of_is_refused_with_the_names_it_takes(call):
    r = call(user_id="anna", as_of="2026-03-01")
    assert r.status_code == 422, r.text
    body = r.json()
    (err,) = body["detail"]
    assert (err["type"], err["loc"]) == ("extra_forbidden", ["query", "as_of"])
    assert "input" not in err
    assert body["message"].startswith("query.as_of is not a parameter here. Did you mean "
                                      "known_as_of? This endpoint takes: ")
    assert "known_as_of" in body["message"] and "token_budget" in body["message"]


def test_every_name_it_takes_still_answers(call):
    r = call(user_id="anna", agent_id="support-bot", token_budget=400,
             known_as_of="2026-03-01", exclude_sessions="true", reader="support-bot",
             changes="all")
    assert r.status_code == 200, r.text


def test_a_name_far_from_any_it_takes_lists_them(call):
    r = call(colour="blue")
    assert r.status_code == 422
    assert r.json()["message"] == (
        "query.colour is not a parameter here. This endpoint takes: agent_id, changes, "
        "exclude_sessions, known_as_of, query, reader, token_budget, user_id")


def test_the_key_and_its_scope_are_asked_first(call, db, tenant):
    assert call(headers={}, as_of="2026-03-01").status_code == 401
    # A key that may not read is told so, not that it sent a wrong name.
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="writes only",
                                            scopes=["memories:write"])
    assert call(headers={"Authorization": f"Bearer {full}"}, as_of="2026-03-01").status_code == 403
