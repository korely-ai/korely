"""One way to write a memory, whatever the door (services/memory_write.py,
audit of 17 Oct 2026).

POST /v1/memories, the MCP korely_add and the batch (its door and its worker)
each wrote a memory by hand and had drifted apart: only MCP refused a NUL
byte in run_id, so REST answered 500 and a batch job carrying one failed on
that item for every attempt, importing nothing after it, with no error to
fix. Here every door refuses the same inputs with the same sentence, and a
memory written through any of them leaves the same audit row and the same
webhook. No model is called.
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from korely_agent.db.models import (AgentMemory, AgentWebhookDelivery, AgentWebhookEndpoint,
                                    AuditEvent)
from korely_agent.mcp import server as mcp_server
from korely_agent.services import batch
from korely_agent.services import keys as key_service
from korely_agent.services.ids import mem_public_id, namespace_id_problem



@pytest.fixture(autouse=True)
def no_extraction(monkeypatch):
    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                        lambda *a, **k: [])


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


def _key(db, tenant, **kw):
    row, full = key_service.create_api_key(db, tenant_id=tenant.id,
                                           name=f"k-{uuid.uuid4().hex[:6]}", **kw)
    return row, full


def mcp(key, tool, args):
    """What the agent reads: the result, or the words of an MCP error (after
    the SDK's own "Error executing tool ...: ")."""
    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(key) is not None
        try:
            return str(await mcp_server._get_agent_mcp().call_tool(tool, args))
        except ToolError as e:
            return str(e).split(": ", 1)[1] if str(e).startswith("Error executing tool") else str(e)
    return asyncio.run(go())


_TIMESTAMP = ("timestamp 'next tuesday' is not an ISO 8601 date or datetime "
              "(e.g. '2026-03-01' or '2026-03-01T14:30:00Z')")

REFUSED = [
    ({"content": "Maria moved to Turin.", "user_id": "u1", "run_id": "a\x00b"},
     "run_id contains a NUL byte (U+0000)"),
    ({"content": "Maria \x00 moved", "user_id": "u1"},
     "content contains a NUL byte (U+0000)"),
    ({"content": "   ", "user_id": "u1"}, "content must not be empty or whitespace"),
    ({"content": "Maria moved to Turin.", "user_id": "u1", "timestamp": "next tuesday"},
     _TIMESTAMP),
]
IDS = ["a NUL byte in run_id", "a NUL byte in the text", "a blank text",
       "an unreadable timestamp"]


class TestEveryDoorRefusesTheSame:
    @pytest.mark.parametrize("item,sentence", REFUSED, ids=IDS)
    def test_rest_mcp_and_the_batch_door(self, db, tenant, client, item, sentence):
        _row, key = _key(db, tenant)
        head = {"Authorization": f"Bearer {key}"}

        r = client.post("/v1/memories", headers=head, json=item)
        assert (r.status_code, r.json()["detail"]) == (422, sentence)

        assert mcp(key, "korely_add", item) == f"error: invalid_request, {sentence}"

        r = client.post("/v1/batch", headers=head, json={"memories": [
            {"content": "fine", "user_id": "u1"}, item]})
        assert (r.status_code, r.json()["detail"]) == (422, f"memories[1].{sentence}")

        db.rollback()
        assert db.query(AgentMemory).count() == 0

    @pytest.mark.parametrize("item,sentence", [
        ({"content": "   ", "user_id": "u1"}, "content must not be empty or whitespace"),
        ({"content": "Maria moved.", "user_id": "u1", "timestamp": "next tuesday"}, _TIMESTAMP),
        ({"content": "Maria moved.", "user_id": "u1", "run_id": "r" * 300},
         "run_id is longer than 255 characters"),
        ({"content": "Maria moved.", "user_id": "acme/42"},
         "user_id " + namespace_id_problem("acme/42")),
    ], ids=["a blank text", "an unreadable timestamp", "a long run_id", "a slash in user_id"])
    def test_the_batch_worker_refuses_the_item_not_the_job(self, db, tenant, item, sentence):
        """A job queued past the door (before a rule existed, or not by the
        API): the item is refused with the door's sentence and the rest of
        the job is imported. A long run_id failed the whole job, for every
        attempt; an unreadable timestamp was imported dated today. (A NUL
        byte cannot be tried here: Postgres refuses to read such an item out
        of the payload, and the door is where it is refused.)"""
        job = batch.enqueue_batch(db, tenant.id, [
            {"content": "first", "user_id": "u1"}, item, {"content": "third", "user_id": "u1"}])
        batch.process_batch_jobs(db, limit=5)
        got = batch.job_status(db, tenant.id, job.id)
        assert got["status"] == "completed"
        assert (got["imported"], got["failed"]) == (2, 1)
        assert got["errors"] == [{"index": 1, "error": sentence}]

    def test_a_confined_key_is_refused_another_end_user_at_every_door(self, db, tenant, client):
        _row, key = _key(db, tenant, end_users=["alice"])
        head = {"Authorization": f"Bearer {key}"}
        sentence = "this key may not read or write user_id 'bob'"
        item = {"content": "Bob likes tea.", "user_id": "bob"}

        r = client.post("/v1/memories", headers=head, json=item)
        assert (r.status_code, r.json()["detail"]) == (403, sentence)
        assert mcp(key, "korely_add", item) == f"error: forbidden, {sentence}"
        r = client.post("/v1/batch", headers=head, json={"memories": [item]})
        assert (r.status_code, r.json()["detail"]) == (403, f"memories[0].user_id: {sentence}")
        db.rollback()
        assert db.query(AgentMemory).count() == 0


    def test_a_fact_written_directly_passes_the_same_gate(self, db, tenant, client):
        """POST /v1/facts takes the ids of a memory write and the same gate:
        a NUL byte in run_id was a 500 there too."""
        _row, key = _key(db, tenant)
        r = client.post("/v1/facts", headers={"Authorization": f"Bearer {key}"}, json={
            "subject": "Maria", "predicate": "lives_in", "object": "Turin",
            "user_id": "u1", "run_id": "a\x00b"})
        assert (r.status_code, r.json()["detail"]) == (422, "run_id contains a NUL byte (U+0000)")


class TestEveryDoorLeavesTheSameTrail:
    def test_the_audit_row_and_the_webhook(self, db, tenant, client):
        row, key = _key(db, tenant)
        db.add(AgentWebhookEndpoint(tenant_id=tenant.id, url="https://example.com/h",
                                    secret="whsec_test", events=["memory.created"]))
        db.commit()
        head = {"Authorization": f"Bearer {key}"}

        r = client.post("/v1/memories", headers={**head, "X-Korely-Client": "claude-code"},
                        json={"content": "Rest lives in Rome.", "user_id": "u-rest",
                              "agent_id": "a1"})
        assert r.status_code == 201
        by_rest = r.json()["id"]
        said = mcp(key, "korely_add", {"content": "Mcp lives in Milan.", "user_id": "u-mcp",
                                       "agent_id": "a1"})
        by_mcp = said.split("Stored ", 1)[1].split(".", 1)[0]
        r = client.post("/v1/batch", headers={**head, "X-Korely-Client": "claude-code"},
                        json={"memories": [{"content": "Batch lives in Bari.",
                                            "user_id": "u-batch", "agent_id": "a1"}]})
        assert r.status_code == 202
        batch.process_batch_jobs(db, limit=5)
        db.rollback()
        by_batch = db.query(AgentMemory).filter(AgentMemory.end_user_id == "u-batch").one()
        by_batch = mem_public_id(by_batch.id)

        rows = {e.target_id: e for e in db.query(AuditEvent).filter(
            AuditEvent.tenant_id == tenant.id).all()
            if (e.meta or {}).get("endpoint") == "memories:add"}
        assert set(rows) == {by_rest, by_mcp, by_batch}
        assert {(rows[m].actor, rows[m].end_user_id, rows[m].action, rows[m].result)
                for m in rows} == {("rest", "u-rest", "write", "ok"),
                                   ("mcp", "u-mcp", "write", "ok"),
                                   ("worker", "u-batch", "write", "ok")}
        # One project for all three: the key's, which queued the job too.
        assert len({rows[m].project_id for m in rows}) == 1
        assert rows[by_rest].api_key_id == rows[by_mcp].api_key_id == row.id
        assert rows[by_rest].meta.get("client") == rows[by_batch].meta.get("client") == "claude-code"

        sent = [d.payload for d in db.query(AgentWebhookDelivery).filter(
            AgentWebhookDelivery.tenant_id == tenant.id,
            AgentWebhookDelivery.event_type == "memory.created").all()]
        assert sorted((p["data"]["id"], p["data"]["user_id"], p["data"]["agent_id"]) for p in sent) == \
            sorted([(by_rest, "u-rest", "a1"), (by_mcp, "u-mcp", "a1"), (by_batch, "u-batch", "a1")])
        assert len({p.get("project_id") for p in sent}) == 1


class TestEveryDoorReadsByTheSameRules:
    """The reads, the same audit's finding: a NUL byte in a filter was a 500
    on REST where MCP refused it, and a blank question a 200 with no results
    on REST where MCP refused it."""

    def test_a_blank_question_is_refused_on_rest_as_on_mcp(self, db, tenant, client):
        _row, key = _key(db, tenant)
        r = client.post("/v1/memories/search", headers={"Authorization": f"Bearer {key}"},
                        json={"query": "   ", "user_id": "u1"})
        assert (r.status_code, r.json()["detail"]) == (422, "query must not be empty")
        assert mcp(key, "korely_search", {"query": "   ", "user_id": "u1"}) == \
            "error: invalid_request, query must not be empty"

    @pytest.mark.parametrize("call", [
        ("get", "/v1/memories", {"params": {"run_id": "a\x00b"}}, "run_id"),
        ("get", "/v1/memories", {"params": {"user_id": "a\x00b"}}, "user_id"),
        ("get", "/v1/events", {"params": {"user_id": "a\x00b"}}, "user_id"),
        ("get", "/v1/facts", {"params": {"user_id": "a\x00b"}}, "user_id"),
        ("post", "/v1/memories/search", {"json": {"query": "tea", "run_id": "a\x00b"}}, "run_id"),
    ], ids=["list run_id", "list user_id", "events user_id", "facts user_id", "search run_id"])
    def test_a_nul_byte_in_a_filter_is_a_refusal(self, db, tenant, client, call):
        method, path, kw, field = call
        _row, key = _key(db, tenant)
        r = getattr(client, method)(path, headers={"Authorization": f"Bearer {key}"}, **kw)
        assert (r.status_code, r.json()["detail"]) == (422, f"{field} contains a NUL byte (U+0000)")

    @pytest.mark.parametrize("tool,args", [
        ("korely_search", {"query": "tea"}), ("korely_get_context", {"query": "tea"}),
        ("korely_get_facts", {})])
    def test_and_on_mcp(self, db, tenant, tool, args):
        _row, key = _key(db, tenant)
        assert mcp(key, tool, {**args, "user_id": "a\x00b"}) == \
            "error: invalid_request, user_id contains a NUL byte (U+0000)"


def test_a_failed_job_keeps_no_text_in_last_error(db, tenant, monkeypatch):
    """A database error quotes the statement's parameters, an item's text
    among them: `last_error` keeps the classes and codes only."""
    def boom(*a, **k):
        raise RuntimeError("Maria's card ends in 4242.")

    monkeypatch.setattr(batch, "_import_one", boom)
    from korely_agent.db.models import AgentBatchJob

    job = batch.enqueue_batch(db, tenant.id, [{"content": "Maria's card ends in 4242."}])
    batch.process_batch_jobs(db, limit=5)
    db.expire_all()
    got = db.get(AgentBatchJob, job.id)
    assert got.status == "failed" and got.last_error
    assert "4242" not in got.last_error and "RuntimeError" in got.last_error
