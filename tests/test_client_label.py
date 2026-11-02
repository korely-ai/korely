"""Which tool wrote a memory: `client`, a label outside the agent cap (15 Oct 2026).

A developer with Claude Code, Codex and Cursor on one memory told them apart
with an agent_id each, and the hosted plans count every agent_id (Hobby: 2).
The client comes from the X-Korely-Client header (REST, batch) or from the
name an MCP client gives in `initialize`; it is stored on the memory, returned
by every read of it, written in the audit trail, and no cap counts it.
"""
import uuid
from types import SimpleNamespace

import pytest

from korely_agent.db.models import AgentMemory, AuditEvent
from korely_agent.services import keys as key_service
from korely_agent.services.clients import client_name



@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient
    from korely_agent import config
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    config.get_settings.cache_clear()
    try:
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()
    finally:
        config.get_settings.cache_clear()


@pytest.fixture
def head(db, tenant):
    _row, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}",
        scopes=["memories:read", "memories:write"])
    return {"Authorization": f"Bearer {full}"}


def test_the_header_is_kept_on_the_memory_and_read_back(client, head, db):
    r = client.post("/v1/memories", headers={**head, "X-Korely-Client": "claude-code"},
                    json={"content": "We ship on Fridays.", "user_id": "repo:shop"})
    assert r.status_code == 201, r.text
    assert r.json()["client"] == "claude-code"
    mem_id = r.json()["id"]
    got = client.get(f"/v1/memories/{mem_id}", headers=head)
    assert got.json()["client"] == "claude-code"
    listed = client.get("/v1/memories", headers=head, params={"user_id": "repo:shop"})
    assert [m["client"] for m in listed.json()["memories"]] == ["claude-code"]
    db.expire_all()
    audit = (db.query(AuditEvent).filter(AuditEvent.target_id == mem_id,
                                         AuditEvent.action == "write").one())
    assert audit.meta["client"] == "claude-code"


def test_no_header_no_client(client, head):
    r = client.post("/v1/memories", headers=head, json={"content": "Plain write."})
    assert r.status_code == 201, r.text
    assert r.json()["client"] is None


def test_a_client_is_not_an_agent(client, head):
    for tool in ("claude-code", "codex", "cursor"):
        r = client.post("/v1/memories", headers={**head, "X-Korely-Client": tool},
                        json={"content": f"Written by {tool}.", "user_id": "repo:shop"})
        assert r.status_code == 201, r.text
    agents = client.get("/v1/agents", headers=head).json()
    assert agents["used"] == 0


def test_a_batch_carries_its_client_to_every_row(client, head, db, tenant):
    from korely_agent.services.batch import process_batch_jobs

    r = client.post("/v1/batch", headers={**head, "X-Korely-Client": "importer"},
                    json={"memories": [{"content": "One."}, {"content": "Two."}]})
    assert r.status_code == 202, r.text
    process_batch_jobs(db)
    db.expire_all()
    rows = db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant.id).all()
    assert sorted(m.client for m in rows) == ["importer", "importer"]


def test_the_label_is_cleaned_never_refused():
    assert client_name("  cursor\x00 \n") == "cursor"
    assert client_name("x" * 100) == "x" * 64
    assert client_name("   ") is None
    assert client_name(None) is None


def test_the_mcp_name_comes_from_initialize():
    from korely_agent.mcp.server import _client_of

    params = SimpleNamespace(clientInfo=SimpleNamespace(name="claude-code", version="2.1"))
    server = SimpleNamespace(get_context=lambda: SimpleNamespace(
        request_context=SimpleNamespace(session=SimpleNamespace(client_params=params))))
    assert _client_of(server) == "claude-code"

    def outside():
        raise ValueError("Context is not available outside of a request")

    assert _client_of(SimpleNamespace(get_context=outside)) is None
