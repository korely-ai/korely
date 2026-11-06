"""The project wall on every /v1 route and every MCP tool (2026-09-28).

A key belongs to one project and sees only that project (core/auth.key_project).
The hosted product found two routes that loaded a row by id filtered on the
account alone, and this install had both, plus a third:

  * GET /v1/memories/{id}/history read another project's timeline (created,
    updated, deleted), and its 200 against a 404 confirmed which memory ids
    exist there;
  * GET /v1/batch/{job_id} read another project's import counts and per-item
    errors, with the same confirmation;
  * GET /v1/audit, which the hosted product does not have, listed every event
    of the account: the end user ids, memory ids and actions of every project,
    filterable by `user_id`. Audit rows now carry the project they were
    produced in, and a key reads its own project's.

This file is the audit made executable: two projects, one key each, and every
route that takes an id or an end user is asked, with the other project's key,
for what the first project wrote. Cross reads are a 404 (or an empty answer
for a listing), cross writes change nothing. The MCP tools are asked the same.
"""
import asyncio
import uuid

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from korely_agent.db.models import AgentBatchJob, AgentFact, AgentMemory, AgentProject, AuditEvent
from korely_agent.services import keys as key_service



@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                        lambda *a, **k: [])


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


def _project(db, tenant, name, default=False):
    p = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name=name, slug=name.lower(),
                     is_default=default)
    db.add(p)
    db.commit()
    return p


def _key(db, tenant, project, scopes=("memories:read", "memories:write")):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id,
                                            name=f"k_{uuid.uuid4().hex[:6]}",
                                            project_id=project.id, scopes=list(scopes))
    return {"Authorization": f"Bearer {full}"}


@pytest.fixture
def world(client, db, tenant):
    """Project Alfa (the default) with a memory, a fact and a batch job; Beta empty."""
    a = _project(db, tenant, "Alfa", default=True)
    b = _project(db, tenant, "Beta")
    ha, hb = _key(db, tenant, a), _key(db, tenant, b)
    mem = client.post("/v1/memories", headers=ha, json={
        "content": "Alfa's secret plan.", "user_id": "anna", "agent_id": "alfa-bot"}).json()
    fact = client.post("/v1/facts", headers=ha, json={
        "subject": "anna", "predicate": "likes", "object": "alfa-tea", "user_id": "anna"}).json()
    job = client.post("/v1/batch", headers=ha, json={"memories": [{"content": "x"}]}).json()
    return {"a": a, "b": b, "ha": ha, "hb": hb,
            "mem": mem["id"], "fact": fact["id"], "job": job["id"]}


# ── the three leaks ────────────────────────────────────────────────────────

class TestTheLeaks:
    def test_history_is_behind_the_wall(self, client, world):
        url = f"/v1/memories/{world['mem']}/history"
        assert client.get(url, headers=world["ha"]).status_code == 200
        r = client.get(url, headers=world["hb"])
        assert r.status_code == 404, r.text
        assert r.json()["detail"] == "Memory not found"

    def test_a_forgotten_memory_keeps_its_timeline_in_its_own_project_only(self, client, world):
        assert client.delete(f"/v1/memories/{world['mem']}", headers=world["ha"]).status_code == 200
        url = f"/v1/memories/{world['mem']}/history"
        events = [e["event"] for e in client.get(url, headers=world["ha"]).json()["events"]]
        assert "deleted" in events
        assert client.get(url, headers=world["hb"]).status_code == 404

    def test_batch_status_is_behind_the_wall(self, client, world):
        url = f"/v1/batch/{world['job']}"
        assert client.get(url, headers=world["ha"]).status_code == 200
        r = client.get(url, headers=world["hb"])
        assert r.status_code == 404, r.text

    def test_the_batch_service_keeps_the_account_level_default(self, db, tenant, world):
        from korely_agent.services import batch
        from korely_agent.services.ids import parse_job_id

        jid = parse_job_id(world["job"])
        assert batch.job_status(db, tenant.id, jid) is not None
        assert batch.job_status(db, tenant.id, jid, project_id=world["b"].id) is None
        assert batch.job_status(db, tenant.id, jid, project_id=None) is None
        assert batch.job_status(db, tenant.id, jid, project_id=world["a"].id) is not None

    def test_the_audit_trail_is_behind_the_wall(self, client, world):
        mine = client.get("/v1/audit", headers=world["ha"]).json()["events"]
        assert "anna" in {e["user_id"] for e in mine}
        theirs = client.get("/v1/audit", headers=world["hb"]).json()
        assert "anna" not in {e["user_id"] for e in theirs["events"]}, theirs
        by_user = client.get("/v1/audit", headers=world["hb"], params={"user_id": "anna"}).json()
        assert by_user == {"events": [], "total": 0}

    def test_an_audit_row_carries_the_project_it_was_produced_in(self, client, db, world):
        client.get("/v1/memories", headers=world["hb"], params={"user_id": "bruno"})
        rows = db.query(AuditEvent).filter(AuditEvent.end_user_id == "bruno").all()
        assert [r.project_id for r in rows] == [world["b"].id]

    def test_the_operator_still_sees_the_whole_account(self, client, db, tenant, world):
        _r, full = key_service.create_api_key(
            db, tenant_id=tenant.id, name="admin",
            scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
        client.get("/v1/memories", headers=world["hb"], params={"user_id": "bruno"})
        seen = {r["user_id"] for r in client.get(
            "/admin/requests", headers={"Authorization": f"Bearer {full}"}).json()["requests"]}
        assert {"anna", "bruno"} <= seen


# ── every other route, asked across the wall ───────────────────────────────

_BY_ID = [
    ("get", "/v1/memories/{mem}", None),
    ("patch", "/v1/memories/{mem}", {"content": "overwritten by beta"}),
    ("delete", "/v1/memories/{mem}", None),
    ("get", "/v1/memories/{mem}/history", None),
    ("post", "/v1/facts/{fact}/forget", None),
    ("patch", "/v1/facts/{fact}", {"object": "beta-coffee"}),
    ("get", "/v1/batch/{job}", None),
    ("delete", "/v1/agents/alfa-bot", None),
]


class TestEveryRouteByIdIs404AcrossTheWall:
    @pytest.mark.parametrize("method,path,body", _BY_ID)
    def test_cross_project(self, client, db, world, method, path, body):
        url = path.format(**world)
        kw = {"json": body} if body is not None else {}
        r = getattr(client, method)(url, headers=world["hb"], **kw)
        assert r.status_code == 404, (method, url, r.status_code, r.text)
        mem = db.query(AgentMemory).filter(AgentMemory.content.like("Alfa%")).one()
        db.refresh(mem)
        assert mem.deleted_at is None and mem.content == "Alfa's secret plan."
        f = db.query(AgentFact).filter(AgentFact.object_label == "alfa-tea").one()
        db.refresh(f)
        assert f.invalid_at is None
        assert db.query(AgentFact).filter(AgentFact.object_label == "beta-coffee").count() == 0


class TestEveryRouteByEndUserSeesOnlyItsProject:
    @pytest.mark.parametrize("url,key", [
        ("/v1/memories?user_id=anna", "memories"),
        ("/v1/memories", "memories"),
        ("/v1/events?user_id=anna", "events"),
        ("/v1/users", "users"),
        ("/v1/agents", "agents"),
        ("/v1/facts?user_id=anna", "facts"),
        ("/v1/facts?entity=anna&include_invalidated=true", "facts"),
        ("/v1/profile?user_id=anna", "facts"),
        ("/v1/audit?user_id=anna", "events"),
    ])
    def test_listings_are_empty_across_the_wall(self, client, world, url, key):
        mine = client.get(url, headers=world["ha"]).json()[key]
        theirs = client.get(url, headers=world["hb"]).json()[key]
        assert mine, url
        assert theirs == [], (url, theirs)

    def test_the_backlog_count(self, client, db, world):
        db.query(AgentMemory).update({AgentMemory.graph_sync_status: "pending"})
        db.commit()
        assert client.get("/v1/events", headers=world["ha"]).json()["processing"] >= 1
        assert client.get("/v1/events", headers=world["hb"]).json()["processing"] == 0

    def test_context_does_not_carry_the_other_project(self, client, world):
        mine = client.get("/v1/context", headers=world["ha"],
                          params={"query": "tea", "user_id": "anna"}).json()
        theirs = client.get("/v1/context", headers=world["hb"],
                            params={"query": "tea", "user_id": "anna"}).json()
        assert "alfa-tea" in mine["context"]
        assert "alfa-tea" not in theirs["context"] and theirs["sources"] == []

    def test_erasing_an_end_user_erases_only_this_project(self, client, db, world):
        body = client.delete("/v1/users/anna/memories", headers=world["hb"]).json()
        assert body["memories_deleted"] == 0 and body["facts_deleted"] == 0
        assert db.query(AgentMemory).filter(AgentMemory.end_user_id == "anna").count() == 1
        assert db.query(AgentFact).filter(AgentFact.end_user_id == "anna").count() == 1

    def test_a_batch_queued_by_beta_is_beta_s(self, client, db, world):
        client.post("/v1/batch", headers=world["hb"], json={"memories": [{"content": "b"}]})
        assert {j.project_id for j in db.query(AgentBatchJob).all()} == {world["a"].id, world["b"].id}


# ── the MCP tools ─────────────────────────────────────────────────────────

def _tool(tenant, project, name, **kwargs):
    from korely_agent.mcp import server as mcp_server

    async def go():
        mcp_server._agent_user_id.set(str(tenant.id))
        mcp_server._agent_project.set(str(project.id))
        mcp_server._agent_scopes.set(("memories:read", "memories:write"))
        try:
            return await mcp_server._get_agent_mcp()._tool_manager._tools[name].fn(**kwargs)
        except ToolError as e:
            return str(e)   # an MCP error since 2026-10-06; the same words

    return asyncio.run(go())


class TestTheMcpToolsAreBehindTheWall:
    def test_facts_and_context(self, tenant, world):
        mine = _tool(tenant, world["a"], "korely_get_facts", user_id="anna")
        theirs = _tool(tenant, world["b"], "korely_get_facts", user_id="anna")
        assert "alfa-tea" in mine and theirs == "no facts."
        ctx = _tool(tenant, world["b"], "korely_get_context", query="tea", user_id="anna")
        assert "alfa-tea" not in ctx

    def test_a_write_lands_in_the_key_project_and_its_audit_row_too(self, db, tenant, world):
        out = _tool(tenant, world["b"], "korely_add", content="Beta's note.", user_id="bruno")
        assert out.startswith("Stored mem_"), out
        row = db.query(AgentMemory).filter(AgentMemory.content == "Beta's note.").one()
        assert row.project_id == world["b"].id
        audit = db.query(AuditEvent).filter(AuditEvent.end_user_id == "bruno").one()
        assert audit.project_id == world["b"].id
        assert audit.actor == "mcp"
