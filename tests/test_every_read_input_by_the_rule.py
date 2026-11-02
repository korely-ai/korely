"""Every input a /v1 read narrows on is asked by the rule of every door
(services/limits.py), so a NUL byte is a 422 with a sentence and never a 500
from the driver (audit of 18 Oct 2026).

0.1.41 closed the same 500 on four read filters and the write path, and left
GET /v1/profile (`user_id`, `agent_id`), GET /v1/audit (`user_id`, `action`),
GET /v1/facts (`subject`, `entity`, `predicate`), DELETE
/v1/users/{end_user}/memories, and MCP's korely_get_facts (`subject`,
`entity`). GET /v1/context took a blank question with a 200 and an empty
block, where MCP's korely_get_context refuses it. No model is called.
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from korely_agent.mcp import server as mcp_server
from korely_agent.services import keys as key_service
from korely_agent.services import limits



@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    # A 500 is an answer here, not an exception out of the client.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def key(db, tenant):
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id,
                                          name=f"k-{uuid.uuid4().hex[:6]}",
                                          scopes=["memories:read", "memories:write"])
    return full


def _mcp(key, tool, args):
    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(key) is not None
        try:
            return str(await mcp_server._get_agent_mcp().call_tool(tool, args))
        except ToolError as e:
            return str(e)
    return asyncio.run(go())


NUL = "a\x00b"


@pytest.mark.parametrize("method,path,params", [
    ("get", "/v1/profile", {"user_id": NUL}),
    ("get", "/v1/profile", {"user_id": "u", "agent_id": NUL}),
    ("get", "/v1/audit", {"user_id": NUL}),
    ("get", "/v1/audit", {"action": NUL}),
    ("get", "/v1/facts", {"subject": NUL}),
    ("get", "/v1/facts", {"entity": NUL}),
    ("get", "/v1/facts", {"predicate": NUL}),
    ("get", "/v1/facts", {"predicate_family": NUL}),
    ("delete", "/v1/users/a%00b/memories", {}),
], ids=["profile user_id", "profile agent_id", "audit user_id", "audit action",
        "facts subject", "facts entity", "facts predicate", "facts family", "erasure"])
def test_a_nul_byte_is_a_422_that_names_it(client, key, method, path, params):
    r = getattr(client, method)(path, params=params, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 422, (path, params, r.status_code, r.text)
    assert r.json()["code"] == "invalid_request"
    assert "NUL" in r.json()["message"]


@pytest.mark.parametrize("field,most", [
    ("subject", limits.MAX_FACT_NAME_CHARS), ("predicate", limits.MAX_FACT_NAME_CHARS),
    ("entity", limits.MAX_FACT_OBJECT_CHARS)])
def test_a_fact_filter_longer_than_its_column_is_refused(client, key, field, most):
    head = {"Authorization": f"Bearer {key}"}
    assert client.get("/v1/facts", params={field: "x" * most}, headers=head).status_code == 200
    r = client.get("/v1/facts", params={field: "x" * (most + 1)}, headers=head)
    assert r.status_code == 422 and field in r.json()["message"], r.text


def test_the_limits_are_the_columns():
    from korely_agent.db import AgentFact

    c = AgentFact.__table__.c
    assert c.subject_label.type.length == limits.MAX_FACT_NAME_CHARS
    assert c.subject_canonical.type.length == limits.MAX_FACT_NAME_CHARS
    assert c.predicate_canonical.type.length == limits.MAX_FACT_NAME_CHARS
    assert c.object_label.type.length == limits.MAX_FACT_OBJECT_CHARS
    assert c.object_canonical.type.length == limits.MAX_FACT_OBJECT_CHARS


def test_an_empty_fact_filter_is_still_no_filter(client, key):
    # `subject=` read as no filter before, and a client that sends it keeps
    # getting the same page.
    r = client.get("/v1/facts", params={"subject": "", "entity": ""},
                   headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("query", ["   ", "a\x00b"], ids=["blank", "a NUL byte"])
def test_the_context_question_is_refused_as_mcp_refuses_it(client, key, query):
    r = client.get("/v1/context", params={"query": query},
                   headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 422, r.text
    assert r.json()["message"] == limits.query_problem(query)
    assert limits.query_problem(query) in _mcp(key, "korely_get_context", {"query": query})


@pytest.mark.parametrize("field", ["subject", "entity"])
def test_mcp_get_facts_asks_the_same_rule(key, field):
    out = _mcp(key, "korely_get_facts", {field: NUL})
    assert "invalid_request" in out and "NUL" in out, out


@pytest.mark.parametrize("path", ["/v1/facts", "/v1/memories", "/v1/audit", "/v1/users",
                                  "/v1/agents", "/admin/memories", "/admin/facts",
                                  "/admin/audit"])
def test_an_offset_past_what_sql_takes_is_a_422(client, db, tenant, path):
    """Audit of 19 Oct 2026: `offset` was bounded below only, and SQL's
    OFFSET holds a bigint, so 2^63 answered 500 on the paged reads; the
    /admin ones took a negative offset to the database as well. Bounded
    by limits.MAX_OFFSET where the page is declared, as `limit` is."""
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name="pager",
                                          scopes=["memories:read", "memories:write", "admin"])
    h = {"Authorization": f"Bearer {full}"}
    for past in (limits.MAX_OFFSET + 1, -1):
        r = client.get(path, params={"offset": past}, headers=h)
        assert r.status_code == 422, (path, past, r.status_code)
    assert client.get(path, params={"offset": limits.MAX_OFFSET}, headers=h).status_code == 200
