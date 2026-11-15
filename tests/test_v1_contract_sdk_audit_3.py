"""The third pass: what the second one listed and left, fixed.

1. PATCH /v1/facts/{id}:
   * a correction that names the fact as it already stands closed the fact
     against itself (`invalidated_by` pointing at its own id), leaving nothing
     true. It reconfirms it now, as POST /v1/facts of the same triple does;
   * the answer's `invalidated` was always `[]`, although the call had just
     closed the fact it was asked to correct: the read shape went through the
     write model, which had no field to carry it. It lists the corrected fact
     and whatever else the contradiction pass on the new one closed, and each
     gets its `fact.invalidated` webhook, as on the write path;
   * a blank field (`"   "`) became a subject made of spaces. It keeps the
     old value.
2. A bad `valid_from` on POST /v1/facts answered "as_of must be ...": the
   message names the field that was sent.
3. The erasure audit (`agent_forget_audit`) was never given a project, so
   deleting a project, which moves every row it holds to the default one,
   never moved these. They carry the project of the rows they account for.
4. An MCP call recorded no `api_key_id` in the audit trail, so "which key did
   this" had no answer for anything done over MCP.
5. The MCP transport started for the first app built in a process and never
   again: `StreamableHTTPSessionManager.run()` runs once per instance and the
   instance was shared, so every later TestClient logged "MCP session manager
   failed to start" and ran without it. conftest.py gives every test a fresh
   server, so the app a test builds starts its own.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import (AgentFact, AgentForgetAudit, AgentMemory, AgentProject,
                                    AuditEvent)
from korely_agent.services import keys as key_service
from korely_agent.services.sentinels import EVERY_PROJECT



@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                        lambda *a, **k: [])


@pytest.fixture
def client(db):
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _project(db, tenant, name, default=False):
    p = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name=name, slug=name.lower(),
                     is_default=default)
    db.add(p)
    db.commit()
    return p


def _key(db, tenant, project=None, scopes=("memories:read", "memories:write")):
    row, full = key_service.create_api_key(db, tenant_id=tenant.id,
                                           name=f"k_{uuid.uuid4().hex[:6]}",
                                           project_id=project.id if project else None,
                                           scopes=list(scopes))
    return row, {"Authorization": f"Bearer {full}"}


@pytest.fixture
def head(db, tenant):
    return _key(db, tenant)[1]


@pytest.fixture
def webhooks(monkeypatch):
    seen = []
    monkeypatch.setattr("korely_agent.services.webhooks.emit_event",
                        lambda db, tenant_id, event, payload: seen.append((event, payload)))
    return seen


def _fact(client, head, obj, subject="Maria", predicate="likes"):
    r = client.post("/v1/facts", headers=head, json={
        "subject": subject, "predicate": predicate, "object": obj, "user_id": "maria"})
    assert r.status_code == 201, r.text
    return r.json()


def _invalidations(seen):
    return [(p["fact_id"], p["invalidated_by"]) for e, p in seen if e == "fact.invalidated"]


# ── 1. a correction says what it superseded, and nothing it did not ───────

class TestTheCorrection:
    def test_invalidated_names_the_corrected_fact(self, client, head, db, webhooks):
        from korely_agent.services import facts as agent_facts

        old = _fact(client, head, "tea")
        r = client.patch(f"/v1/facts/{old['id']}", headers=head, json={"object": "coffee"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["invalidated"] == [old["id"]]
        assert body["object"] == "coffee" and body["id"] != old["id"]
        new = db.query(AgentFact).filter(AgentFact.object_label == "coffee").one()
        read = agent_facts.serialize_fact(new)
        for k in ("id", "subject", "predicate", "object", "predicate_family", "valid_from",
                  "tense", "invalid_at", "observation_count"):
            assert body[k] == read[k], k
        assert _invalidations(webhooks) == [(old["id"], body["id"])], "one event per closed fact"

    def test_it_lists_what_the_contradiction_pass_closed_too(
            self, client, head, db, webhooks, monkeypatch):
        """The new fact goes through the same contradiction pass as any
        write, and what that closes is part of what the correction did."""
        from korely_agent.db.models import AgentFact as F
        from korely_agent.services.facts import contradiction

        monkeypatch.setattr(contradiction, "_stage1_candidates",
                            lambda db_, tenant_id, end_user_id, *a, exclude_id=None, **k:
                            db_.query(F).filter(F.tenant_id == tenant_id,
                                                F.invalid_at.is_(None),
                                                F.id != exclude_id).all())
        monkeypatch.setattr(contradiction, "_classify",
                            lambda candidates, *a, **k: ([c.id for c in candidates], {}))
        old = _fact(client, head, "tea")
        other = _fact(client, head, "black tea", predicate="drinks")
        # The second write's own pass closed the first: open it again, so the
        # correction below is the only thing that closes anything.
        db.query(F).update({F.invalid_at: None, F.invalidated_by_id: None})
        db.commit()
        webhooks.clear()

        body = client.patch(f"/v1/facts/{old['id']}", headers=head,
                            json={"object": "coffee"}).json()
        assert body["invalidated"] == [old["id"], other["id"]], body
        assert sorted(_invalidations(webhooks)) == sorted(
            [(old["id"], body["id"]), (other["id"], body["id"])])

    def test_naming_the_fact_as_it_stands_supersedes_nothing(self, client, head, db, webhooks):
        old = _fact(client, head, "tea")
        webhooks.clear()
        r = client.patch(f"/v1/facts/{old['id']}", headers=head, json={"object": "tea"})
        assert r.status_code == 200, r.text
        assert r.json()["invalidated"] == [] and r.json()["id"] == old["id"]
        row = db.query(AgentFact).one()
        db.refresh(row)
        assert row.invalid_at is None and row.invalidated_by_id is None, "closed against itself"
        assert _invalidations(webhooks) == []
        assert client.get("/v1/facts", headers=head, params={"user_id": "maria"}).json()["total"] == 1

    def test_the_dashboard_correction_does_not_close_it_against_itself_either(
            self, client, head, db, tenant):
        from korely_agent.services.facts import service

        old = _fact(client, head, "tea")
        out = service.correct_fact(db, tenant.id, uuid.UUID(old["id"][4:]), obj="tea", project_id=EVERY_PROJECT)
        assert out["id"] == old["id"] and "subject_type" in out, "the read shape, for the screen"
        row = db.query(AgentFact).one()
        db.refresh(row)
        assert row.invalid_at is None and row.invalidated_by_id is None

    def test_a_blank_field_keeps_the_old_value(self, client, head):
        old = _fact(client, head, "tea")
        body = client.patch(f"/v1/facts/{old['id']}", headers=head,
                            json={"subject": "   ", "predicate": " ", "object": "coffee"}).json()
        assert (body["subject"], body["object"]) == ("Maria", "coffee"), body
        assert body["predicate"] == old["predicate"]

    def test_only_blank_fields_is_still_refused(self, client, head):
        old = _fact(client, head, "tea")
        r = client.patch(f"/v1/facts/{old['id']}", headers=head, json={"object": "  "})
        assert r.status_code == 422 and r.json()["code"] == "invalid_request"


# ── 2. the error names the field that was wrong ──────────────────────────

class TestTheErrorNamesTheField:
    def test_valid_from(self, client, head):
        r = client.post("/v1/facts", headers=head, json={
            "subject": "Maria", "predicate": "likes", "object": "tea", "valid_from": "garbage",
            "user_id": "maria"})
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == "valid_from must be an ISO date (2026-06-01) or datetime"
        assert "as_of" not in r.text

    def test_as_of_and_at_still_name_themselves(self, client, head):
        r = client.get("/v1/facts", headers=head, params={"as_of": "garbage"})
        assert r.json()["detail"].startswith("as_of must be")
        fid = _fact(client, head, "tea")["id"]
        r = client.post(f"/v1/facts/{fid}/forget", headers=head, json={"at": "garbage"})
        assert r.json()["detail"].startswith("at must be")


# ── 3. the erasure audit belongs to a project, and moves with it ──────────

class TestTheErasureAuditHasAProject:
    @pytest.fixture
    def beta(self, db, tenant):
        _project(db, tenant, "Default", default=True)
        return _project(db, tenant, "Beta")

    def test_every_row_carries_the_project_it_accounts_for(self, client, db, tenant, beta):
        _row, hb = _key(db, tenant, beta)
        mem = client.post("/v1/memories", headers=hb, json={"content": "x", "user_id": "anna",
                                                            "agent_id": "bot"}).json()
        client.delete(f"/v1/memories/{mem['id']}", headers=hb)
        f1 = _fact(client, hb, "tea")
        client.post(f"/v1/facts/{f1['id']}/forget", headers=hb)
        f2 = _fact(client, hb, "coffee")
        client.patch(f"/v1/facts/{f2['id']}", headers=hb, json={"object": "water"})
        client.post("/v1/memories", headers=hb, json={"content": "y", "user_id": "anna",
                                                      "agent_id": "bot"})
        client.delete("/v1/agents/bot", headers=hb)
        client.delete("/v1/users/anna/memories", headers=hb)
        rows = db.query(AgentForgetAudit).all()
        assert sorted(r.scope for r in rows) == ["agent", "fact", "fact", "memory", "user"]
        assert {r.project_id for r in rows} == {beta.id}

    def test_the_dashboard_files_them_under_the_row_s_project(self, client, db, tenant, beta):
        from korely_agent.services.facts import service

        _row, hb = _key(db, tenant, beta)
        fid = uuid.UUID(_fact(client, hb, "tea")["id"][4:])
        service.forget_fact(db, tenant.id, fid, project_id=EVERY_PROJECT)
        assert [r.project_id for r in db.query(AgentForgetAudit).all()] == [beta.id]

    def test_deleting_the_project_moves_them(self, client, db, tenant, beta):
        from korely_agent.services import projects as project_service

        _row, hb = _key(db, tenant, beta)
        fid = _fact(client, hb, "tea")["id"]
        client.post(f"/v1/facts/{fid}/forget", headers=hb)
        client.delete("/v1/users/maria/memories", headers=hb)
        default = project_service.get_or_create_default_project(db, tenant.id)
        moved = project_service.delete_project(db, tenant.id, beta)
        assert moved["audit"] == 2, moved
        db.expire_all()
        assert {r.project_id for r in db.query(AgentForgetAudit).all()} == {default.id}


# ── 4 and 5. over the MCP transport, which every app now starts ───────────

def _rpc(response):
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"no JSON-RPC message in: {response.text[:200]}")


def _mcp_session(c, head):
    head = {**head, "Accept": "application/json, text/event-stream"}
    r = c.post("/mcp/", headers=head, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}}})
    assert r.status_code == 200, r.text[:300]
    head["mcp-session-id"] = r.headers["mcp-session-id"]
    c.post("/mcp/", headers=head, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return head


def _mcp_call(c, head, tool, args):
    r = c.post("/mcp/", headers=head, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool, "arguments": args}})
    assert r.status_code == 200, r.text[:300]
    return _rpc(r)["result"]


class TestTheMcpAuditNamesTheKey:
    def test_a_write_and_a_read_over_mcp(self, client, db, tenant):
        beta = _project(db, tenant, "Beta")
        row, hb = _key(db, tenant, beta)
        head = _mcp_session(client, hb)
        out = _mcp_call(client, head, "korely_add", {"content": "Maria likes tea.",
                                                     "user_id": "maria"})
        assert not out.get("isError"), out
        _mcp_call(client, head, "korely_get_facts", {"user_id": "maria"})
        db.rollback()
        events = db.query(AuditEvent).filter(AuditEvent.actor == "mcp").all()
        assert sorted(e.meta["endpoint"] for e in events) == ["facts:list", "memories:add"]
        assert {e.api_key_id for e in events} == {row.id}
        assert {e.project_id for e in events} == {beta.id}
        mem = db.query(AgentMemory).one()
        assert mem.project_id == beta.id


class TestEveryAppStartsItsMcpTransport:
    @pytest.mark.parametrize("attempt", [1, 2, 3])
    def test_the_transport_answers(self, caplog, client, head, attempt):
        """Three in a row, so at least two run after an app was already
        started in this process, which is where it used to fail."""
        failed = [r for r in caplog.get_records("setup")
                  if "MCP session manager failed to start" in r.getMessage()]
        assert failed == [], "the app started without its MCP transport"
        session = _mcp_session(client, head)
        assert "mcp-session-id" in session
