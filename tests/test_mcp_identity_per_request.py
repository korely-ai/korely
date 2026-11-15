"""Over MCP, every call is made with the key of its own request (review of
2026-10-06, the same flaw found in the hosted product).

The session manager lets a session be used only by the principal that opened
it, and compares `AccessToken.client_id`: this server set it to the tenant,
so any key of the account could use a session another key opened. And the
tools read the identity from context variables set when the session's first
request was verified: a read-only key used through a read-and-write key's
session wrote, and a key moved to another project kept reading the old one.
Now `client_id` is the key, and each call takes its identity from its own
HTTP request. Real HTTP through the mounted transport; no model is called.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentMemory
from korely_agent.services import keys as key_service



@pytest.fixture
def client(db, monkeypatch):
    from korely_agent.config import get_settings
    from korely_agent.mcp import server as mcp_server

    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
    get_settings.cache_clear()
    mcp_server._agent_mcp_singleton = None
    mcp_server._agent_streamable_app_singleton = None
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()
    mcp_server._agent_mcp_singleton = None
    mcp_server._agent_streamable_app_singleton = None


def _key(db, tenant, scopes, project_id=None):
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id,
                                          name=f"k-{uuid.uuid4().hex[:6]}",
                                          scopes=scopes, project_id=project_id)
    return _r, full


def _post(c, key, body, sid=None):
    head = {"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"}
    if sid:
        head["mcp-session-id"] = sid
    return c.post("/mcp/", json=body, headers=head)


def _payload(r):
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        datas = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
        return json.loads(datas[-1]) if datas else None
    return r.json() if r.text else None


def _open(c, key):
    r = _post(c, key, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "1"}}})
    assert r.status_code == 200, r.text[:200]
    sid = r.headers["mcp-session-id"]
    _post(c, key, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    return sid


def _call(c, key, sid, tool, args):
    return _post(c, key, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": tool, "arguments": args}}, sid)


class TestASessionBelongsToItsKey:
    def test_a_read_only_key_cannot_write_through_another_keys_session(self, db, tenant,
                                                                       client):
        _rw_row, rw = _key(db, tenant, ["memories:read", "memories:write"])
        _ro_row, ro = _key(db, tenant, ["memories:read"])
        sid = _open(client, rw)
        r = _call(client, ro, sid, "korely_add", {"content": "must not land", "user_id": "u1"})
        assert r.status_code == 404, f"{r.status_code}: {r.text[:200]}"
        db.rollback()
        assert db.query(AgentMemory).filter(AgentMemory.content == "must not land").count() == 0

    def test_the_key_that_opened_it_still_works(self, db, tenant, client):
        _row, rw = _key(db, tenant, ["memories:read", "memories:write"])
        sid = _open(client, rw)
        r = _call(client, rw, sid, "korely_add", {"content": "it lands", "user_id": "u1"})
        assert r.status_code == 200 and _payload(r)["result"]["isError"] is False


class TestEachCallReadsItsKeyNow:
    def test_a_key_moved_to_another_project_reads_the_new_one(self, db, tenant, client):
        from korely_agent.services import projects as project_service

        row, rw = _key(db, tenant, ["memories:read", "memories:write"])
        sid = _open(client, rw)
        other = project_service.create_project(db, tenant_id=tenant.id, name="Other")
        assert key_service.move_key(db, key_id=row.id, tenant_id=tenant.id, project_id=other.id)
        r = _call(client, rw, sid, "korely_add", {"content": "where am I", "user_id": "u1"})
        assert r.status_code == 200, r.text[:200]
        db.expire_all()
        m = db.query(AgentMemory).filter(AgentMemory.content == "where am I").one()
        assert m.project_id == other.id, "the call wrote into the project the key had before"


class TestARefusalIsAnMcpError:
    def test_an_empty_memory_is_is_error(self, db, tenant, client):
        _row, rw = _key(db, tenant, ["memories:read", "memories:write"])
        sid = _open(client, rw)
        r = _call(client, rw, sid, "korely_add", {"content": "   ", "user_id": "u1"})
        result = _payload(r)["result"]
        assert result["isError"] is True, result
        assert "content must not be empty" in result["content"][0]["text"]
