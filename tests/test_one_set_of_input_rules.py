"""The input rules are the same at every door: REST, /v1/batch and MCP
(services/limits.py; the audit of 2026-10-06, row 5). MCP took 20,000
characters, a NUL byte and an unbounded run_id and question; the batch took
metadata the single write refuses. No model is called.
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from korely_agent.db.models import AgentMemory
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
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def key(db, tenant):
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}")
    return full


def mcp(key, tool, args):
    """What the agent reads: the result, or the words of an MCP error."""
    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(key) is not None
        try:
            return str(await mcp_server._get_agent_mcp().call_tool(tool, args))
        except ToolError as e:
            return str(e)
    return asyncio.run(go())


LONG = "x" * (limits.MAX_CONTENT_CHARS + 1)


class TestTheThreeDoorsAgree:
    @pytest.mark.parametrize("content", [LONG, "Maria \x00 moved", "   "],
                             ids=["too long", "a NUL byte", "blank"])
    def test_a_text_refused_by_rest_is_refused_by_mcp_and_the_batch(self, db, client, key,
                                                                     content):
        head = {"Authorization": f"Bearer {key}"}
        assert client.post("/v1/memories", headers=head,
                           json={"content": content, "user_id": "u1"}).status_code == 422
        assert client.post("/v1/batch", headers=head,
                           json={"memories": [{"content": content, "user_id": "u1"}]}
                           ).status_code == 422
        assert "error: invalid_request" in mcp(key, "korely_add",
                                               {"content": content, "user_id": "u1"})
        db.rollback()
        assert db.query(AgentMemory).count() == 0

    def test_metadata_the_single_write_refuses_the_batch_refuses(self, client, key):
        head = {"Authorization": f"Bearer {key}"}
        big = {"blob": "y" * (limits.MAX_METADATA_BYTES + 1)}
        assert client.post("/v1/memories", headers=head, json={
            "content": "hi", "user_id": "u1", "metadata": big}).status_code == 422
        assert client.post("/v1/batch", headers=head, json={"memories": [
            {"content": "hi", "user_id": "u1", "metadata": big}]}).status_code == 422

    @pytest.mark.parametrize("char", ["a", "\u00e8", "\u0436", "\u3042", "\U0001f600"],
                             ids=["ascii", "latin", "cyrillic", "japanese", "emoji"])
    def test_the_metadata_limit_is_bytes_in_every_alphabet(self, char):
        """8192 bytes of UTF-8 for every alphabet: measured on the ASCII-escaped
        JSON, Japanese was refused at about 4,100 bytes and Cyrillic at a third
        of the limit (audit of 17 Oct 2026)."""
        import json

        def size(n):
            return len(json.dumps({"note": char * n}, ensure_ascii=False).encode("utf-8"))

        n = 1
        while size(n + 1) <= limits.MAX_METADATA_BYTES:
            n += 1
        assert limits.metadata_problem({"note": char * n}) is None
        assert limits.metadata_problem({"note": char * (n + 1)}) is not None

    def test_a_lone_surrogate_is_refused_not_raised_on(self):
        assert "surrogate" in limits.metadata_problem({"note": "\ud800"})

    def test_a_long_run_id_on_mcp(self, key):
        said = mcp(key, "korely_add", {"content": "hi", "user_id": "u1", "run_id": "r" * 300})
        assert "run_id is longer than 255" in said

    @pytest.mark.parametrize("tool", ["korely_get_context", "korely_search"])
    def test_a_question_too_long_on_mcp(self, key, tool):
        said = mcp(key, tool, {"query": "q" * (limits.MAX_QUERY_CHARS + 1), "user_id": "u1"})
        assert "error: invalid_request" in said and "query is" in said
