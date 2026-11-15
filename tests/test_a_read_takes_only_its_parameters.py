"""Every GET of /v1 refuses a query parameter it does not take (review of 14
Nov 2026).

`GET /v1/facts?userId=anna` (the JavaScript spelling) and `GET
/v1/audit?userid=anna` answered as if nothing had been asked, with every end
user of the key; `as_of` on GET /v1/context answered with the facts of
today. A body refuses an unknown field, the MCP's tools and both SDKs an
unknown option; the reads now refuse one too, each named, after the key. The
one name a client of ours sends that a route does not take (`run_id`, the n8n
node 0.1.0's, on the context and the facts) is taken and not read.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.services import context as ctx
from korely_agent.services import keys as key_service


def takes_only_its_parameters(route) -> bool:
    """Whether the route's dependencies include the rule
    (api/_bodies.parameters_it_takes)."""
    stack = [route.dependant]
    while stack:
        d = stack.pop()
        if getattr(d.call, "takes_only_its_parameters", False):
            return True
        stack.extend(d.dependencies)
    return False


def get_routes(app) -> list:
    from korely_agent.api.errors import every_route

    return [r for r in every_route(app.routes)
            if "GET" in (getattr(r, "methods", None) or ()) and r.path.startswith("/v1")]


@pytest.fixture
def app(monkeypatch):
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    return create_app()


@pytest.fixture
def client(app, db, monkeypatch):
    from korely_agent.db import get_db

    monkeypatch.setattr(ctx, "embed", lambda *a, **k: None)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def head(db, tenant, scopes=None):
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name=uuid.uuid4().hex[:8],
                                          scopes=scopes)
    return {"Authorization": f"Bearer {full}"}


def the_key_is_asked_before_the_parameters(client, routes, write_only: dict) -> list:
    """For each GET route and each caller the route refuses for who it is
    (no key, a wrong key, a key without the read scope, `write_only`):
    (path, caller, the answer without a name the route does not take, the
    answer with one). The key and its scope first: such a caller is told
    that, whatever it sends."""
    import re

    out = []
    for r in routes:
        path = re.sub(r"\{[^}]+\}", "x", r.path)
        for who, headers in (("no key", {}),
                             ("wrong key", {"Authorization": "Bearer kor_self_not-a-key"}),
                             ("write only", write_only)):
            plain = client.get(path, headers=headers).status_code
            extra = client.get(path, headers=headers, params={"notAParameter": "1"}).status_code
            out.append((path, who, plain, extra))
    return out


def refused_for_who_they_are_whatever_they_send(answers) -> None:
    refused = [a for a in answers if a[2] in (401, 403)]
    assert [a for a in refused if a[3] != a[2]] == []
    assert {a[2] for a in answers if a[1] != "write only"} == {401}
    # A key without the read scope is refused by every read but the ping,
    # which any valid key may call.
    assert {a[0] for a in answers if a[1] == "write only" and a[2] != 403} == {"/v1/ping"}


def write_only_key(db, tenant) -> dict:
    return head(db, tenant, scopes=["memories:write"])


def test_without_a_key_that_may_read_a_name_it_does_not_take_changes_nothing(
        app, client, db, tenant):
    """Every GET of /v1, found as the guard below finds them (mutant R6 of
    the re-check of 14 Nov 2026: the rule moved before the key of /v1/facts
    survived 180 tests)."""
    answers = the_key_is_asked_before_the_parameters(client, get_routes(app),
                                                     write_only_key(db, tenant))
    assert len(answers) >= 36
    refused_for_who_they_are_whatever_they_send(answers)


def test_every_get_of_v1_has_the_rule(app):
    routes = get_routes(app)
    assert len(routes) >= 12, "the walk found fewer GET routes than the core has"
    missing = sorted(r.path for r in routes if not takes_only_its_parameters(r))
    assert missing == [], f"GET routes that take any parameter in silence: {missing}"


@pytest.mark.parametrize("path,params", [
    ("/v1/facts", {"userId": "anna"}),
    ("/v1/audit", {"userid": "anna"}),
    ("/v1/memories", {"user": "anna"}),
    ("/v1/users", {"user_id": "anna"}),
    ("/v1/ping", {"x": "1"}),
    ("/v1/profile", {"userId": "anna"}),
    ("/v1/events", {"since": "2026-01-01"}),
    ("/v1/agents", {"agent_id": "bot"}),
], ids=lambda v: v if isinstance(v, str) else ",".join(v))
def test_a_name_it_does_not_take_is_refused(db, tenant, client, path, params):
    r = client.get(path, headers=head(db, tenant), params=params)
    assert r.status_code == 422, r.text
    (name,) = params
    assert [e["loc"] for e in r.json()["detail"]] == [["query", name]]
    assert r.json()["message"].startswith(f"query.{name} is not a parameter here.")


def test_every_name_it_does_not_take_is_named(db, tenant, client):
    r = client.get("/v1/facts", headers=head(db, tenant),
                   params={"userId": "anna", "asOf": "2026-01-01", "user_id": "anna"})
    assert r.status_code == 422
    assert [e["loc"] for e in r.json()["detail"]] == [["query", "userId"], ["query", "asOf"]]
    assert r.json()["message"].endswith("(and 1 more)")


@pytest.mark.parametrize("path,params", [
    ("/v1/context", {"query": "plan", "run_id": "r1"}),
    ("/v1/facts", {"run_id": "r1"}),
], ids=["context", "facts"])
def test_the_n8n_node_0_1_0s_run_id_still_answers(db, tenant, client, path, params):
    assert client.get(path, headers=head(db, tenant), params=params).status_code == 200


def test_a_route_without_parameters_refuses_one_after_its_key(db, tenant, client):
    assert client.get("/v1/ping", params={"x": "1"}).status_code == 401
    assert client.get("/v1/ping", headers=head(db, tenant)).status_code == 200
