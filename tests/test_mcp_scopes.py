"""A key does over MCP exactly what it may do over REST.

The MCP transport asked for `memories:read` and nothing else, and the tools
checked nothing. A key minted read-only got a 403 from POST /v1/memories and
wrote the same memory with korely_add. A key with no scopes at all was read by
the MCP verifier as holding read and write, where REST reads it as holding
nothing. And a write-only key was refused korely_add, which its REST twin
allows.

Every tool now needs the scope its REST twin needs, from one table
(mcp/server.py, TOOL_SCOPES) checked at the one place every tools/call arrives.
The central test below does not trust the table: for each tool and each shape
of key it asks REST and MCP the same question and requires the same answer.
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from mcp import types
from mcp.server.fastmcp.exceptions import ToolError

from korely_agent.db.models import AgentMemory
from korely_agent.mcp import server as mcp_server
from korely_agent.services import keys as key_service


READ, WRITE = "memories:read", "memories:write"
KEY_SHAPES = {
    "no scopes": [],
    "read only": [READ],
    "write only": [WRITE],
    "read and write": [READ, WRITE],
}

# Each tool, its REST twin, and one call of each.
TWINS = {
    "korely_get_context": (("GET", "/v1/context", {"params": {"query": "plan", "user_id": "u1"}}),
                           {"query": "plan", "user_id": "u1"}),
    "korely_add": (("POST", "/v1/memories", {"json": {"content": "scope probe", "user_id": "u1"}}),
                   {"content": "scope probe", "user_id": "u1"}),
    "korely_search": (("POST", "/v1/memories/search", {"json": {"query": "plan"}}),
                      {"query": "plan"}),
    "korely_get_facts": (("GET", "/v1/facts", {"params": {"user_id": "u1"}}),
                         {"user_id": "u1"}),
    "korely_correct_fact": (("PATCH", "/v1/facts/fct_" + "0" * 32, {"json": {"object": "x"}}),
                            {"fact_id": "fct_" + "0" * 32, "object": "x"}),
    "korely_forget_memory": (("DELETE", "/v1/memories/mem_" + "0" * 32, {}),
                             {"memory_id": "mem_" + "0" * 32}),
}


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


def key_with(db, tenant, scopes):
    _row, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:8]}", scopes=scopes)
    return full


def mcp_call(key, tool, args):
    """(refused_for_scope, text) of one call through the MCP server's own
    dispatch, after verifying `key` the way the transport does."""
    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(key) is not None
        try:
            out = await mcp_server._get_agent_mcp().call_tool(tool, args)
        except ToolError as e:
            return "missing required scope" in str(e), str(e)
        return False, str(out)
    return asyncio.run(go())


class TestTheSameAnswerThroughBothDoors:
    @pytest.mark.parametrize("shape", list(KEY_SHAPES))
    @pytest.mark.parametrize("tool", list(TWINS))
    def test_rest_and_mcp_agree(self, client, db, tenant, shape, tool):
        key = key_with(db, tenant, KEY_SHAPES[shape])
        (method, path, kwargs), args = TWINS[tool]
        r = client.request(method, path, headers={"Authorization": f"Bearer {key}"}, **kwargs)
        assert r.status_code != 401, r.text
        rest_refused = r.status_code == 403
        mcp_refused, text = mcp_call(key, tool, args)
        assert mcp_refused == rest_refused, (
            f"{shape} key, {tool}: REST {'refuses' if rest_refused else 'allows'} "
            f"({r.status_code}), MCP {'refuses' if mcp_refused else 'allows'}: {text[:120]}")


class TestAReadOnlyKey:
    def test_cannot_write_over_mcp(self, db, tenant):
        key = key_with(db, tenant, [READ])
        refused, text = mcp_call(key, "korely_add", {"content": "must not land", "user_id": "u1"})
        assert refused, text
        assert "memories:write" in text, "the refusal should say which scope is missing"
        db.rollback()
        assert db.query(AgentMemory).filter(AgentMemory.content == "must not land").count() == 0

    def test_can_still_read_over_mcp(self, db, tenant):
        key = key_with(db, tenant, [READ])
        refused, text = mcp_call(key, "korely_get_facts", {"user_id": "u1"})
        assert not refused, text

    def test_is_refused_through_the_server_s_own_handler(self, db, tenant):
        """The handler the server runs for tools/call, not the method under
        it: if the library ever stopped dispatching through `call_tool`, the
        check would be bypassed and this would say so."""
        key = key_with(db, tenant, [READ])
        server = mcp_server._get_agent_mcp()._mcp_server
        handler = server.request_handlers[types.CallToolRequest]
        req = types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(
                name="korely_add", arguments={"content": "must not land", "user_id": "u1"}))

        async def go():
            await mcp_server.KorelyAgentKeyVerifier().verify_token(key)
            return await handler(req)
        result = asyncio.run(go()).root
        assert result.isError is True
        assert "memories:write" in result.content[0].text


class TestWhatAKeyIsGiven:
    def test_a_key_with_no_scopes_holds_none(self, db, tenant):
        key = key_with(db, tenant, [])
        token = asyncio.run(mcp_server.KorelyAgentKeyVerifier().verify_token(key))
        assert token is not None and token.scopes == [], \
            "a key given nothing was read as holding read and write"

    def test_a_write_only_key_can_write_as_over_rest(self, db, tenant):
        key = key_with(db, tenant, [WRITE])
        refused, text = mcp_call(key, "korely_add", {"content": "write only", "user_id": "u1"})
        assert not refused, text


class TestTheTable:
    def test_the_door_itself_asks_for_no_scope(self):
        """The calls above go through the server's dispatch, not through the
        HTTP transport in front of it, so they cannot see a scope demanded at
        the door. It demanded `memories:read`, which refused a write-only key
        the korely_add its REST twin allows. Which scope a call needs depends
        on the tool, so the door asks only that the key is valid."""
        assert not mcp_server._get_agent_mcp().settings.auth.required_scopes

    def test_every_tool_declares_its_scope(self):
        registered = {t.name for t in mcp_server._get_agent_mcp()._tool_manager.list_tools()}
        assert registered == set(mcp_server.TOOL_SCOPES), (
            f"tools without a scope: {registered - set(mcp_server.TOOL_SCOPES)}; "
            f"scopes for no tool: {set(mcp_server.TOOL_SCOPES) - registered}")

    def test_a_tool_missing_from_it_is_refused_to_every_key(self, db, tenant, monkeypatch):
        monkeypatch.delitem(mcp_server.TOOL_SCOPES, "korely_get_facts")
        key = key_with(db, tenant, [READ, WRITE])

        async def go():
            await mcp_server.KorelyAgentKeyVerifier().verify_token(key)
            with pytest.raises(ToolError, match="declares no scope"):
                await mcp_server._get_agent_mcp().call_tool("korely_get_facts", {})
        asyncio.run(go())


class TestOverHttp:
    """The whole way in: the HTTP transport, the session, then the tool.

    Everything above calls the server's dispatch in-process. Over HTTP the
    scopes travel the way the tenant does, set by the verifier on the request
    that opens the session and read in the session's own task. If they did not
    arrive, every call would be refused, read ones included, and only this
    test would notice.
    """

    @pytest.fixture
    def http(self, db, monkeypatch):
        monkeypatch.setenv("RUN_WORKER", "false")
        mcp_server._agent_mcp_singleton = None
        mcp_server._agent_streamable_app_singleton = None
        from korely_agent.db import get_db
        from korely_agent.main import create_app

        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                yield c
        finally:
            mcp_server._agent_mcp_singleton = None
            mcp_server._agent_streamable_app_singleton = None

    @staticmethod
    def _rpc(response):
        """The JSON-RPC message in a response, plain JSON or one SSE event."""
        import json

        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError(f"no JSON-RPC message in: {response.text[:200]}")

    def _session(self, c, key):
        head = {"Authorization": f"Bearer {key}",
                "Accept": "application/json, text/event-stream"}
        r = c.post("/mcp/", headers=head, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}})
        assert r.status_code == 200, r.text[:200]
        head["mcp-session-id"] = r.headers["mcp-session-id"]
        c.post("/mcp/", headers=head,
               json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        return head

    def _call(self, c, head, tool, args):
        r = c.post("/mcp/", headers=head, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": args}})
        assert r.status_code == 200, r.text[:200]
        return self._rpc(r)["result"]

    def test_a_read_only_key_reads_and_does_not_write(self, http, db, tenant):
        head = self._session(http, key_with(db, tenant, [READ]))
        read = self._call(http, head, "korely_get_facts", {"user_id": "u1"})
        assert not read.get("isError"), read
        wrote = self._call(http, head, "korely_add",
                           {"content": "must not land over http", "user_id": "u1"})
        assert wrote.get("isError") is True, wrote
        assert "memories:write" in wrote["content"][0]["text"]
        db.rollback()
        assert db.query(AgentMemory).filter(
            AgentMemory.content == "must not land over http").count() == 0


class TestTheRateLimitHasNoSideDoor:
    """RATE_LIMIT_PER_MINUTE counted REST and not MCP: with 2 a minute REST
    answered 429 to the third call and MCP took ten of ten (the audit of
    2026-10-06, row 4). One counter per tenant for both doors."""

    @pytest.fixture
    def two_a_minute(self, monkeypatch):
        from korely_agent.config import get_settings

        monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def test_mcp_is_refused_after_the_ceiling(self, db, tenant, two_a_minute):
        key = key_with(db, tenant, [READ])
        answers = [mcp_call(key, "korely_get_facts", {"user_id": "u1"})[1] for _ in range(4)]
        assert ["rate_limit_exceeded" in a for a in answers] == [False, False, True, True]

    def test_rest_and_mcp_share_the_counter(self, client, db, tenant, two_a_minute):
        key = key_with(db, tenant, [READ])
        head = {"Authorization": f"Bearer {key}"}
        assert client.get("/v1/facts", params={"user_id": "u1"}, headers=head).status_code == 200
        assert "rate_limit_exceeded" not in mcp_call(key, "korely_get_facts", {"user_id": "u1"})[1]
        assert client.get("/v1/facts", params={"user_id": "u1"}, headers=head).status_code == 429

    def test_rest_and_mcp_say_it_in_the_same_words(self, client, db, tenant, two_a_minute):
        """One 429, built once (core/auth.rate_limited, audit of 17 Oct 2026):
        REST said "Rate limit exceeded (2/min)" and MCP "rate limit exceeded
        (2 requests a minute)", without the code."""
        key = key_with(db, tenant, [READ])
        head = {"Authorization": f"Bearer {key}"}
        for _ in range(2):
            client.get("/v1/facts", params={"user_id": "u1"}, headers=head)
        r = client.get("/v1/facts", params={"user_id": "u1"}, headers=head)
        assert r.status_code == 429 and r.json()["code"] == "rate_limit_exceeded"
        rest = r.json()["message"]
        assert rest.startswith("Rate limit exceeded (2/min): try again in "), rest
        text = mcp_call(key, "korely_get_facts", {"user_id": "u1"})[1]
        assert text.startswith("error: rate_limit_exceeded, Rate limit exceeded (2/min): "
                               "try again in "), text

    def test_a_ceiling_from_the_hook_counts_on_mcp_too(self, db, tenant):
        """Whether a ceiling applies is the limiter's to say: MCP decided it
        again from RATE_LIMIT_PER_MINUTE and KORELY_MODE, so a rate_limits
        hook outside the hosted mode was skipped there and kept over REST."""
        from korely_agent.core import extensions

        extensions.register("rate_limits", lambda db, tenant_id: {"minute": 1})
        try:
            key = key_with(db, tenant, [READ])
            answers = [mcp_call(key, "korely_get_facts", {"user_id": "u1"})[1] for _ in range(2)]
        finally:
            extensions.reset()
        assert ["rate_limit_exceeded" in a for a in answers] == [False, True]

    def test_off_by_default_counts_nothing(self, db, tenant):
        from korely_agent.db.models import AgentRateLimit

        key = key_with(db, tenant, [READ])
        for _ in range(3):
            assert "rate_limit_exceeded" not in mcp_call(key, "korely_get_facts", {"user_id": "u1"})[1]
        assert db.query(AgentRateLimit).count() == 0
