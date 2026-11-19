"""The API's document says what three answers hold (review of 14-15 Nov 2026).

- DELETE /v1/memories/{memory_id} and DELETE /v1/agents/{agent_id} answer
  `facts_reopened` (the facts the deletion made true again, 31 Oct 2026),
  and the document gave both answers no fields at all: a client generated
  from it, or the SDKs read against it, could not know the count was there.
  Each now has its model, with the fields in the order on the wire, and the
  answers are the ones of before, byte for byte in their keys.
- Every GET of /v1 refuses a query parameter it does not take (1.0.23,
  api/_bodies.parameters_it_takes), and the document's 422 said only "a
  query parameter is malformed". The 422 of those operations now says it,
  with the name a client of ours sends that a route takes and does not read.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentMemory
from korely_agent.services import keys as key_service


@pytest.fixture
def app(db, monkeypatch):
    from korely_agent.main import create_app
    from korely_agent.mcp import server as mcp_server

    monkeypatch.setenv("RUN_WORKER", "false")
    mcp_server._agent_mcp_singleton = None
    mcp_server._agent_streamable_app_singleton = None
    yield create_app()
    mcp_server._agent_mcp_singleton = None
    mcp_server._agent_streamable_app_singleton = None


@pytest.fixture
def spec(app):
    return json.loads(json.dumps(app.openapi()))


@pytest.fixture
def client(app, db):
    from korely_agent.db import get_db

    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _answer(spec, path, method, status="200"):
    return spec["paths"][path][method]["responses"][status]


def _model(spec, path, method):
    ref = _answer(spec, path, method)["content"]["application/json"]["schema"]["$ref"]
    return spec["components"]["schemas"][ref.rsplit("/", 1)[1]]


class TestTheDeletionsSayTheirFields:
    def test_a_memorys_deletion(self, spec):
        props = _model(spec, "/v1/memories/{memory_id}", "delete")["properties"]
        assert list(props) == ["id", "status", "erasure", "facts_invalidated",
                               "facts_deleted", "facts_reopened", "audit_id"]
        assert "true again" in props["facts_reopened"]["description"]

    def test_an_agents_deletion(self, spec):
        props = _model(spec, "/v1/agents/{agent_id}", "delete")["properties"]
        assert list(props) == ["agent_id", "memories_deleted", "facts_deleted",
                               "facts_reopened", "read_marks_deleted", "audit_id"]
        assert "true again" in props["facts_reopened"]["description"]


def _key(db, tenant):
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name=uuid.uuid4().hex[:8])
    return {"Authorization": f"Bearer {full}"}


def _memory(db, tenant, agent="bot"):
    from korely_agent.core.auth import key_project
    from korely_agent.db.models import AgentApiKey

    project = key_project(db, db.query(AgentApiKey).filter(
        AgentApiKey.tenant_id == tenant.id).first())
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project,
                    end_user_id="u1", agent_id=agent, content="something")
    db.add(m)
    db.commit()
    from korely_agent.services.ids import mem_public_id

    return mem_public_id(m.id)


class TestTheAnswersAreTheOnesOfBefore:
    """The model filters nothing and adds nothing: the keys each answer had,
    in their order, and no `erasure` or `facts_deleted` in a forget's."""

    def test_a_forget(self, db, tenant, client):
        head = _key(db, tenant)
        r = client.delete(f"/v1/memories/{_memory(db, tenant)}", headers=head)
        assert r.status_code == 200, r.text
        assert list(r.json()) == ["id", "status", "facts_invalidated", "facts_reopened",
                                  "audit_id"]

    def test_an_erasure(self, db, tenant, client):
        head = _key(db, tenant)
        r = client.delete(f"/v1/memories/{_memory(db, tenant)}", params={"erase": "true"},
                          headers=head)
        assert r.status_code == 200, r.text
        assert list(r.json()) == ["id", "status", "erasure", "facts_invalidated",
                                  "facts_deleted", "facts_reopened", "audit_id"]
        assert r.json()["erasure"] == "permanent"

    def test_an_agents_deletion(self, db, tenant, client):
        head = _key(db, tenant)
        _memory(db, tenant, agent="bot-z")
        r = client.delete("/v1/agents/bot-z", headers=head)
        assert r.status_code == 200, r.text
        assert list(r.json()) == ["agent_id", "memories_deleted", "facts_deleted",
                                  "facts_reopened", "read_marks_deleted", "audit_id"]
        assert r.json()["memories_deleted"] == 1


class TestTheUnknownParameterIsInThe422:
    def test_every_get_that_refuses_one_says_it(self, app, spec):
        from korely_agent.api._bodies import the_parameters_rule
        from korely_agent.api.errors import api_routes

        said = 0
        for route in api_routes(app.routes, gated=False):
            if not route.path.startswith("/v1") or "GET" not in route.methods:
                continue
            text = _answer(spec, route.path_format, "get", "422")["description"]
            if the_parameters_rule(route) is not None:
                assert "does not take is refused too" in text, route.path
                said += 1
        # Counted from an independent source: every GET of /v1 has the rule
        # (tests/test_a_read_takes_only_its_parameters.py), so none is skipped.
        assert said >= 10, said

    def test_a_write_says_nothing_of_it(self, spec):
        text = _answer(spec, "/v1/memories", "post", "422")["description"]
        assert "does not take is refused too" not in text

    def test_the_name_taken_and_not_read_is_named(self, spec):
        text = _answer(spec, "/v1/facts", "get", "422")["description"]
        assert "`run_id` (the n8n node 0.1.0 sends it" in text
        assert "`run_id`" not in _answer(spec, "/v1/memories", "get", "422")["description"]

    def test_the_answer_is_the_one_the_document_says(self, db, tenant, client):
        r = client.get("/v1/facts", params={"notAParameter": "1"}, headers=_key(db, tenant))
        assert r.status_code == 422, r.text
        (err,) = r.json()["detail"]
        assert err["loc"] == ["query", "notAParameter"] and err["type"] == "extra_forbidden"
        assert "This endpoint takes:" in err["msg"]
