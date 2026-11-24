"""A key that holds `admin` is served over MCP, and its session is told so.

A test from outside, 23 Nov 2026: the key an account gets before it is
connected to a login holds `admin`, and an MCP client connected with it was
served with no word, while the guide says a key in an MCP client must not
hold it. None of the six tools needs `admin`; refusing the key would cut off
every client set up with one, so the answer to `initialize` says it after
the server's instructions (mcp/server._admin_key_note), and only for such a
key. On an install of one's own the way forward is another key; an
account the hosted service lets make no keys yet has its own
(core/extensions, `admin_key_way_forward`).
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.core import extensions, threads
from korely_agent.mcp import server as mcp_server
from korely_agent.services import keys as key_service

READ, WRITE, ADMIN = "memories:read", "memories:write", key_service.ADMIN_SCOPE


@pytest.fixture
def http(db, monkeypatch):
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


def _key(db, tenant, scopes):
    _row, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:8]}", scopes=scopes)
    return full


def _rpc(response):
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"no JSON-RPC message in: {response.text[:200]}")


def _opened(c, key):
    """The `initialize` result of a new session, and its headers."""
    head = {"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"}
    r = c.post("/mcp/", headers=head, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}}})
    assert r.status_code == 200, r.text[:200]
    head["mcp-session-id"] = r.headers["mcp-session-id"]
    c.post("/mcp/", headers=head, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return _rpc(r)["result"], head


def test_an_admin_key_is_served_and_told_it_should_not_be_here(http, db, tenant):
    result, head = _opened(http, _key(db, tenant, [READ, WRITE, ADMIN]))
    assert result["instructions"] == (
        f"{mcp_server._AGENT_MCP_INSTRUCTIONS}\n{mcp_server._AGENT_MCP_ADMIN_KEY_NOTE} "
        f"{mcp_server._AGENT_MCP_ANOTHER_KEY}")
    # What `admin` opens on every install: no route of the Cloud alone.
    assert "keys, its projects, its webhooks, the erasure of an end user" in result["instructions"]
    assert "delet" not in result["instructions"]
    # Served as before: the warning refuses nothing.
    r = http.post("/mcp/", headers=head, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "korely_get_facts", "arguments": {"user_id": "u1"}}})
    assert r.status_code == 200 and not _rpc(r)["result"].get("isError"), r.text[:300]


@pytest.mark.parametrize("scopes", [[READ, WRITE], [READ]], ids=["read-write", "read-only"])
def test_a_key_without_admin_gets_the_instructions_alone(http, db, tenant, scopes):
    result, _head = _opened(http, _key(db, tenant, scopes))
    assert result["instructions"] == mcp_server._AGENT_MCP_INSTRUCTIONS


def test_each_session_is_answered_for_its_own_key(http, db, tenant):
    """The warning is the opening key's, never a session's before it."""
    admin, plain = _key(db, tenant, [READ, WRITE, ADMIN]), _key(db, tenant, [READ, WRITE])
    seen = [_opened(http, k)[0]["instructions"] for k in (admin, plain, admin, plain)]
    note = mcp_server._AGENT_MCP_ADMIN_KEY_NOTE
    assert [note in s for s in seen] == [True, False, True, False]


def test_the_note_is_read_on_an_admission_thread(http, db, tenant, monkeypatch):
    """The account's way forward is read from the database: on the admission
    pool (core/threads.admission_off_the_loop), never on the loop, which
    every session of every account shares."""
    seen = []

    def way_forward(session, tenant_id):
        seen.append((threads._ADMITTING.get(), tenant_id))
        return None

    monkeypatch.setitem(extensions.hooks.__dict__, "admin_key_way_forward", way_forward)
    _opened(http, _key(db, tenant, [READ, WRITE, ADMIN]))
    assert seen == [(True, tenant.id)], seen
    # A key without `admin` reads nothing for its note.
    _opened(http, _key(db, tenant, [READ, WRITE]))
    assert len(seen) == 1, seen
