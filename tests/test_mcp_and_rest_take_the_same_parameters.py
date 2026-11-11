"""The same operation takes the same parameters over MCP and REST.

korely_search said 10 results by default where POST /v1/memories/search says
15, cut a `limit` of 500 to 50 where REST answers 422, and showed 200
characters of each memory where REST's `snippet` shows 280. korely_get_context
read a `token_budget` of 0 as 800 and cut 9,000 to 8,000; korely_get_facts cut
its `limit` to 1..200. And several MCP refusals carried no code, so an agent
had to read a sentence to tell a refusal from a failure (audit of 17 Oct 2026).
The numbers live in services/limits.py and services/context.py
now, and every refusal of the tools is `error: <code>, <sentence>`.
"""
import asyncio
import inspect
import uuid

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from korely_agent.mcp import server as mcp_server
from korely_agent.services import keys as key_service
from korely_agent.services import limits
from korely_agent.services.context import MAX_TOKEN_BUDGET, MIN_TOKEN_BUDGET
from korely_agent.services.memory_engine import MAX_PAGE



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
def key(db, tenant):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id,
                                            name=f"k-{uuid.uuid4().hex[:6]}")
    return full


def mcp(key, tool, args):
    """What the agent reads: the result, or the words of the error after the
    SDK's own "Error executing tool ...: "."""
    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(key) is not None
        try:
            return str(await mcp_server._get_agent_mcp().call_tool(tool, args))
        except ToolError as e:
            s = str(e)
            return s.split(": ", 1)[1] if s.startswith("Error executing tool") else s
    return asyncio.run(go())


def _default(tool, name):
    fn = mcp_server._get_agent_mcp()._tool_manager.get_tool(tool).fn
    return inspect.signature(fn).parameters[name].default


def test_the_defaults_are_rests():
    from korely_agent.api.memories import SearchBody

    assert _default("korely_search", "limit") == SearchBody.model_fields["limit"].default \
        == limits.DEFAULT_SEARCH_RESULTS


@pytest.mark.parametrize("tool,args,route", [
    ("korely_search", {"query": "plan", "limit": limits.MAX_SEARCH_RESULTS + 1},
     ("POST", "/v1/memories/search", {"json": {"query": "plan",
                                               "limit": limits.MAX_SEARCH_RESULTS + 1}})),
    ("korely_search", {"query": "plan", "limit": 0},
     ("POST", "/v1/memories/search", {"json": {"query": "plan", "limit": 0}})),
    ("korely_get_context", {"query": "plan", "token_budget": 0},
     ("GET", "/v1/context", {"params": {"query": "plan", "token_budget": 0}})),
    ("korely_get_context", {"query": "plan", "token_budget": MAX_TOKEN_BUDGET + 1},
     ("GET", "/v1/context", {"params": {"query": "plan", "token_budget": MAX_TOKEN_BUDGET + 1}})),
    ("korely_get_facts", {"limit": MAX_PAGE + 1},
     ("GET", "/v1/facts", {"params": {"limit": MAX_PAGE + 1}})),
    ("korely_get_facts", {"as_of": "yesterday"},
     ("GET", "/v1/facts", {"params": {"as_of": "yesterday"}})),
    ("korely_search", {"query": "plan", "limit": "many"},
     ("POST", "/v1/memories/search", {"json": {"query": "plan", "limit": "many"}})),
])
def test_what_rest_refuses_mcp_refuses_with_the_same_code(client, key, tool, args, route):
    method, path, kw = route
    r = client.request(method, path, headers={"Authorization": f"Bearer {key}"}, **kw)
    assert r.status_code == 422 and r.json()["code"] == "invalid_request", r.text
    said = mcp(key, tool, args)
    assert said.startswith("error: invalid_request, "), said


def test_the_bounds_inside_are_taken(client, key, monkeypatch):
    from korely_agent.services import context, memory_engine

    for module in (memory_engine, context):
        monkeypatch.setattr(module, "embed", lambda *a, **k: [0.1] * 768)
    assert not mcp(key, "korely_search", {"query": "plan",
                                          "limit": limits.MAX_SEARCH_RESULTS}).startswith("error")
    assert not mcp(key, "korely_get_context", {"query": "plan",
                                               "token_budget": MIN_TOKEN_BUDGET}).startswith("error")
    assert not mcp(key, "korely_get_facts", {"limit": MAX_PAGE}).startswith("error")


def test_a_hit_shows_as_much_of_the_memory_on_both_doors(client, db, key, monkeypatch):
    from korely_agent.services import memory_engine

    monkeypatch.setenv("WRITE_ASYNC", "true")
    monkeypatch.setattr(memory_engine, "embed", lambda *a, **k: [0.1] * 768)
    head = {"Authorization": f"Bearer {key}"}
    long = "Ada " + "x" * 600
    assert client.post("/v1/memories", headers=head, json={"content": long}).status_code == 201
    snippet = client.post("/v1/memories/search", headers=head,
                          json={"query": "Ada"}).json()["results"][0]["snippet"]
    assert len(snippet) == limits.SNIPPET_CHARS
    assert snippet in mcp(key, "korely_search", {"query": "Ada"})
    assert long[:limits.SNIPPET_CHARS + 1] not in mcp(key, "korely_search", {"query": "Ada"})


def test_every_refusal_of_a_tool_carries_a_code(db, tenant, key, monkeypatch):
    """A scope missing, the search unavailable: `error: <code>, ...`."""
    from korely_agent.services import memory_engine

    _row, read_only = key_service.create_api_key(db, tenant_id=tenant.id, name="ro",
                                                 scopes=["memories:read"])
    assert mcp(read_only, "korely_add", {"content": "x"}).startswith("error: forbidden, ")
    monkeypatch.setattr(memory_engine, "embed", lambda *a, **k: None)
    assert mcp(key, "korely_search", {"query": "plan"}).startswith("error: search_unavailable, ")


# What a REST twin takes that its tool leaves out on purpose: a run and the
# stored metadata are REST's finer filters, a page offset is a list's. Any
# other parameter of the twin is the tool's too, so a narrowing added to one
# door and not the other fails here: korely_add wrote under an agent_id that
# no MCP read could narrow on, and korely_get_facts had no `predicate`
# (audit of 19 Oct 2026).
_ONLY_REST = {"korely_search": {"run_id", "metadata"}, "korely_get_facts": {"offset"},
              "korely_get_context": set()}


def _rest_parameters(tool):
    from korely_agent.api.context import get_context
    from korely_agent.api.facts import list_facts
    from korely_agent.api.memories import SearchBody

    if tool == "korely_search":
        return set(SearchBody.model_fields)
    route = {"korely_get_context": get_context, "korely_get_facts": list_facts}[tool]
    return set(inspect.signature(route).parameters) - {"key", "db"}


@pytest.mark.parametrize("tool", sorted(_ONLY_REST))
def test_a_tool_takes_every_parameter_of_its_rest_twin(tool):
    fn = mcp_server._get_agent_mcp()._tool_manager.get_tool(tool).fn
    missing = _rest_parameters(tool) - _ONLY_REST[tool] - set(inspect.signature(fn).parameters)
    assert not missing, f"{tool} lacks {sorted(missing)}, which its REST twin takes"


@pytest.mark.parametrize("tool,args,route", [
    ("korely_search", {"query": "plan", "agent_id": ""},
     ("POST", "/v1/memories/search", {"json": {"query": "plan", "agent_id": ""}})),
    ("korely_get_context", {"query": "plan", "agent_id": ""},
     ("GET", "/v1/context", {"params": {"query": "plan", "agent_id": ""}})),
    ("korely_get_facts", {"agent_id": ""}, ("GET", "/v1/facts", {"params": {"agent_id": ""}})),
    # The reader of the agent notices, an id by the same rule (1.0.15).
    ("korely_get_context", {"query": "plan", "reader": ""},
     ("GET", "/v1/context", {"params": {"query": "plan", "reader": ""}})),
    ("korely_get_facts", {"reader": ""}, ("GET", "/v1/facts", {"params": {"reader": ""}})),
    ("korely_get_facts", {"predicate": "p" * (limits.MAX_FACT_NAME_CHARS + 1)},
     ("GET", "/v1/facts", {"params": {"predicate": "p" * (limits.MAX_FACT_NAME_CHARS + 1)}})),
])
def test_the_narrowings_are_refused_as_rest_refuses_them(client, key, tool, args, route):
    method, path, kw = route
    r = client.request(method, path, headers={"Authorization": f"Bearer {key}"}, **kw)
    assert r.status_code == 422 and r.json()["code"] == "invalid_request", r.text
    said = mcp(key, tool, args)
    assert said.startswith("error: invalid_request, "), said
    assert r.json()["message"] in said, (r.json()["message"], said)


def test_a_read_narrows_on_the_agent_and_the_predicate_as_rest_does(client, key, monkeypatch):
    """What korely_add writes under an agent_id an MCP read can read back
    alone, and korely_get_facts narrows on the relation."""
    from korely_agent.services import context, memory_engine

    monkeypatch.setenv("WRITE_ASYNC", "true")
    for module in (memory_engine, context):
        monkeypatch.setattr(module, "embed", lambda *a, **k: [0.1] * 768)
    head = {"Authorization": f"Bearer {key}"}
    for agent, note in (("support-bot", "Ada prefers email"), ("sales-bot", "Ada asked for a quote")):
        assert client.post("/v1/memories", headers=head,
                           json={"content": note, "agent_id": agent}).status_code == 201
    for agent, pred, obj in (("support-bot", "works_at", "Acme"),
                             ("sales-bot", "lives_in", "Turin")):
        assert client.post("/v1/facts", headers=head, json={
            "subject": "Ada", "predicate": pred, "object": obj, "agent_id": agent}).status_code == 201

    hits = mcp(key, "korely_search", {"query": "Ada", "agent_id": "support-bot"})
    assert "Ada prefers email" in hits and "quote" not in hits, hits
    block = mcp(key, "korely_get_context", {"query": "Ada", "agent_id": "support-bot"})
    assert "Ada prefers email" in block and "quote" not in block, block
    for args, shown, hidden in (({"agent_id": "support-bot"}, "Acme", "Turin"),
                                ({"predicate": "lives_in"}, "Turin", "Acme")):
        facts = mcp(key, "korely_get_facts", args)
        rest = client.get("/v1/facts", headers=head, params=args).json()["facts"]
        assert [f["object"] for f in rest] == [shown]
        assert shown in facts and hidden not in facts, facts
