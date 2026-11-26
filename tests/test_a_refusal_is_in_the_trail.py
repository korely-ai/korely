"""A refused /v1 call is in the trail, and every read says what it showed
(CONTRIBUTING.md, the audit trail; audit of 18 Oct 2026).

Every refusal raised before a route's one log_v1_request left no row, so an
access request answered from the trail missed the refused attempts on a
person's data; only memories:get and memories:delete wrote theirs. GET
/v1/events listed memory ids with their end users and wrote neither, and
GET /v1/batch/{id} did not name the job. GET /v1/ping wrote a row per call
with no ceiling, so a key past its ceiling kept growing the shared table.
No model is called.
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from korely_agent.db import AuditEvent
from korely_agent.mcp import server as mcp_server
from korely_agent.services import keys as key_service



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
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id,
                                          name=f"k-{uuid.uuid4().hex[:6]}",
                                          scopes=["memories:read", "memories:write"])
    return full


@pytest.fixture
def head(key):
    return {"Authorization": f"Bearer {key}"}


def _rows(db, tenant, endpoint):
    db.expire_all()
    return [r for r in db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id)
            .order_by(AuditEvent.ts, AuditEvent.id).all()
            if (r.meta or {}).get("endpoint") == endpoint]


def _memory(client, head, user="u1"):
    r = client.post("/v1/memories", json={"content": "Maria lives in Rome", "user_id": user},
                    headers=head)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


class TestARefusalIsKept:
    def test_a_fact_that_is_not_there(self, client, db, tenant, head):
        fid = "fct_" + uuid.uuid4().hex
        assert client.post(f"/v1/facts/{fid}/forget", headers=head).status_code == 404
        assert client.patch(f"/v1/facts/{fid}", json={"object": "Milan"},
                            headers=head).status_code == 404
        for endpoint in ("facts:forget", "facts:correct"):
            (row,) = _rows(db, tenant, endpoint)
            assert (row.result, row.target_id, row.meta["code"]) == ("error", fid, "not_found")

    def test_a_fact_closed_before_it_began(self, client, db, tenant, head):
        r = client.post("/v1/facts", json={"subject": "maria", "predicate": "lives_in",
                                           "object": "Rome", "user_id": "u1",
                                           "valid_from": "2026-06-01"}, headers=head)
        assert r.status_code == 201, r.text
        fid = r.json()["id"]
        r = client.post(f"/v1/facts/{fid}/forget", json={"at": "2026-01-01"}, headers=head)
        assert r.status_code == 422, r.text
        (row,) = _rows(db, tenant, "facts:forget")
        assert (row.result, row.end_user_id, row.target_id) == ("error", "u1", fid)
        assert row.meta["code"] == "invalid_request"

    def test_a_memory_history_and_an_edit_of_nothing(self, client, db, tenant, head):
        mid = "mem_" + uuid.uuid4().hex
        assert client.get(f"/v1/memories/{mid}/history", headers=head).status_code == 404
        assert client.patch(f"/v1/memories/{mid}", json={"content": "x"},
                            headers=head).status_code == 404
        for endpoint in ("memories:history", "memories:update"):
            (row,) = _rows(db, tenant, endpoint)
            assert (row.result, row.target_id, row.meta["code"]) == ("error", mid, "not_found")

    def test_a_stale_edit_names_the_memory_and_its_end_user(self, client, db, tenant, head):
        mid = _memory(client, head)
        r = client.patch(f"/v1/memories/{mid}", headers=head,
                         json={"content": "Maria lives in Milan",
                               "expected_updated_at": "2020-01-01T00:00:00Z"})
        assert r.status_code == 409, r.text
        (row,) = _rows(db, tenant, "memories:update")
        assert (row.result, row.end_user_id, row.target_id) == ("error", "u1", mid)
        assert row.meta["code"] == "stale_write"

    def test_a_search_that_could_not_run(self, client, db, tenant, head, key, monkeypatch):
        from korely_agent.services.memory_engine import AgentMemoryEngine, MemorySearchUnavailable

        def down(*a, **k):
            raise MemorySearchUnavailable("down")

        monkeypatch.setattr(AgentMemoryEngine, "search", down)
        r = client.post("/v1/memories/search", json={"query": "where", "user_id": "u1"},
                        headers=head)
        assert r.status_code == 503, r.text

        async def go():
            assert await mcp_server.KorelyAgentKeyVerifier().verify_token(key) is not None
            with pytest.raises(ToolError):
                await mcp_server._get_agent_mcp().call_tool(
                    "korely_search", {"query": "where", "user_id": "u1"})
        asyncio.run(go())
        rows = _rows(db, tenant, "memories:search")
        assert [(r.actor, r.result, r.end_user_id, r.meta["code"]) for r in rows] == [
            ("rest", "error", "u1", "search_unavailable"),
            ("mcp", "error", "u1", "search_unavailable")]

    def test_a_context_that_could_not_be_read(self, db, tenant, key, monkeypatch):
        # korely_get_context answers search_unavailable when the block came
        # back degraded and empty: its row says `error`, with the code, as
        # korely_search's does (audit of 19 Oct 2026); it said `ok`.
        from korely_agent.services.memory_engine import AgentMemoryEngine, MemorySearchUnavailable

        def down(*a, **k):
            raise MemorySearchUnavailable("down")

        monkeypatch.setattr(AgentMemoryEngine, "search", down)

        async def go():
            assert await mcp_server.KorelyAgentKeyVerifier().verify_token(key) is not None
            with pytest.raises(ToolError, match="search_unavailable"):
                await mcp_server._get_agent_mcp().call_tool(
                    "korely_get_context", {"query": "where", "user_id": "u1"})
        asyncio.run(go())
        (row,) = _rows(db, tenant, "context")
        assert (row.actor, row.result, row.end_user_id, row.meta["code"]) == (
            "mcp", "error", "u1", "search_unavailable")
        assert row.read_ids is None

    def test_a_batch_job_is_named(self, client, db, tenant, head):
        r = client.post("/v1/batch", json={"memories": [{"content": "a note", "user_id": "u1"}]},
                        headers=head)
        assert r.status_code in (200, 201, 202), r.text
        job = r.json()["id"]
        assert client.get(f"/v1/batch/{job}", headers=head).status_code == 200
        other = job[:-4] + ("0000" if not job.endswith("0000") else "1111")
        assert client.get(f"/v1/batch/{other}", headers=head).status_code == 404
        ok, missing = _rows(db, tenant, "batch:get")
        assert (ok.result, ok.target_id) == ("ok", job)
        assert (missing.result, missing.target_id, missing.meta["code"]) == (
            "error", other, "not_found")


@pytest.fixture
def confined(db, tenant):
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id,
                                          name=f"c-{uuid.uuid4().hex[:6]}",
                                          scopes=["memories:read", "memories:write"],
                                          end_users=["u1"])
    return full


class TestAnAttemptOnAnotherPersonIsKept:
    """The 403 of a key confined to some end users that names another one,
    by REST and by MCP, and the refusals of the erasures: each left no row
    (audit of 19 Oct 2026), the very attempts refused() was written for."""

    def _refused(self, db, tenant, endpoint, actor="rest"):
        rows = [r for r in _rows(db, tenant, endpoint) if r.actor == actor]
        assert rows, f"no row for {endpoint} ({actor})"
        (row,) = rows
        return row

    def test_every_read_of_another_end_user(self, client, db, tenant, confined):
        head = {"Authorization": f"Bearer {confined}"}
        calls = {
            "memories:list": lambda: client.get("/v1/memories", params={"user_id": "u2"},
                                                headers=head),
            "events:list": lambda: client.get("/v1/events", params={"user_id": "u2"},
                                              headers=head),
            "facts:list": lambda: client.get("/v1/facts", params={"user_id": "u2"},
                                             headers=head),
            "profile": lambda: client.get("/v1/profile", params={"user_id": "u2"},
                                          headers=head),
            "context": lambda: client.get("/v1/context", params={"query": "where",
                                                                 "user_id": "u2"},
                                          headers=head),
            "memories:search": lambda: client.post("/v1/memories/search", headers=head,
                                                   json={"query": "where", "user_id": "u2"}),
            "audit:read": lambda: client.get("/v1/audit", params={"user_id": "u2"},
                                             headers=head),
        }
        for endpoint, call in calls.items():
            r = call()
            assert r.status_code == 403, (endpoint, r.text)
            row = self._refused(db, tenant, endpoint)
            assert (row.result, row.end_user_id, row.meta["code"]) == (
                "error", "u2", "forbidden"), endpoint

    def test_every_write_for_another_end_user(self, client, db, tenant, confined):
        head = {"Authorization": f"Bearer {confined}"}
        calls = {
            "memories:add": lambda: client.post("/v1/memories", headers=head, json={
                "content": "Maria lives in Rome", "user_id": "u2"}),
            "facts:create": lambda: client.post("/v1/facts", headers=head, json={
                "subject": "maria", "predicate": "lives_in", "object": "Rome",
                "user_id": "u2"}),
            "names:declare": lambda: client.post("/v1/users/u2/names", headers=head,
                                                 json={"name": "Maria Rossi"}),
            "batch:create": lambda: client.post("/v1/batch", headers=head, json={
                "memories": [{"content": "a note", "user_id": "u2"}]}),
            "users:delete": lambda: client.delete("/v1/users/u2/memories", headers=head),
        }
        for endpoint, call in calls.items():
            r = call()
            assert r.status_code == 403, (endpoint, r.text)
            row = self._refused(db, tenant, endpoint)
            assert (row.result, row.end_user_id, row.meta["code"]) == (
                "error", "u2", "forbidden"), endpoint

    def test_every_tool_for_another_end_user(self, db, tenant, confined):
        async def go():
            assert await mcp_server.KorelyAgentKeyVerifier().verify_token(confined) is not None
            server = mcp_server._get_agent_mcp()
            for tool, args in (("korely_get_context", {"query": "where", "user_id": "u2"}),
                               ("korely_search", {"query": "where", "user_id": "u2"}),
                               ("korely_get_facts", {"user_id": "u2"}),
                               ("korely_add", {"content": "Maria lives in Rome",
                                               "user_id": "u2"})):
                with pytest.raises(ToolError, match="forbidden"):
                    await server.call_tool(tool, args)
        asyncio.run(go())
        for endpoint in ("context", "memories:search", "facts:list", "memories:add"):
            row = self._refused(db, tenant, endpoint, actor="mcp")
            assert (row.result, row.end_user_id, row.meta["code"]) == (
                "error", "u2", "forbidden"), endpoint

    def test_an_agent_namespace_not_deleted(self, client, db, tenant, head, confined):
        r = client.delete("/v1/agents/support-bot",
                          headers={"Authorization": f"Bearer {confined}"})
        assert r.status_code == 403, r.text
        assert client.delete("/v1/agents/nobody", headers=head).status_code == 404
        forbidden, missing = _rows(db, tenant, "agents:delete")
        assert (forbidden.result, forbidden.target_id, forbidden.meta["code"]) == (
            "error", "support-bot", "forbidden")
        assert (missing.result, missing.target_id, missing.meta["code"]) == (
            "error", "nobody", "not_found")

    def test_a_pseudonym_is_not_erased_again(self, client, db, tenant, head):
        from korely_agent.services.ids import ERASED_PREFIX, is_pseudonym

        erased = ERASED_PREFIX + uuid.uuid4().hex  # the shape an erasure leaves
        assert is_pseudonym(erased)
        r = client.delete(f"/v1/users/{erased}/memories", headers=head)
        assert r.status_code == 422, r.text
        (row,) = _rows(db, tenant, "users:delete")
        assert (row.result, row.end_user_id, row.meta["code"]) == (
            "error", erased, "invalid_request")

    @pytest.mark.parametrize("erase", [False, True])
    def test_a_well_formed_id_that_names_nothing(self, client, db, tenant, head, erase):
        """Audit of 20 Oct 2026: the deletion's 404 wrote `error` with no
        code, the one /v1 refusal not built with api/errors.refused."""
        from korely_agent.services.ids import mem_public_id

        mid = mem_public_id(uuid.uuid4())
        r = client.delete(f"/v1/memories/{mid}", headers=head,
                          params={"erase": "true"} if erase else None)
        assert r.status_code == 404, r.text
        (row,) = _rows(db, tenant, "memories:delete")
        assert (row.result, row.target_id, row.meta["code"]) == ("error", mid, "not_found")
        assert bool(row.meta.get("erase")) is erase

    def test_every_v1_refusal_past_validation_is_in_the_trail(self):
        """A /v1 route raises a refusal other than a 422 of validation only
        through api/errors.refused, so it has its row and its code."""
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "korely_agent" / "api"
        bare = []
        for name in ("memories.py", "facts.py", "context.py", "batch.py", "audit.py", "ping.py"):
            for node in ast.walk(ast.parse((root / name).read_text())):
                if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                        and getattr(node.exc.func, "id", None) == "HTTPException":
                    src = ast.unparse(node.exc)
                    if "422" not in src:
                        bare.append(f"{name}:{node.lineno} {src[:80]}")
        assert bare == [], bare

    def test_a_memory_id_that_is_not_one(self, client, db, tenant, head):
        assert client.get("/v1/memories/not-a-memory-id", headers=head).status_code == 404
        assert client.delete("/v1/memories/not-a-memory-id", headers=head).status_code == 404
        for endpoint in ("memories:get", "memories:delete"):
            (row,) = _rows(db, tenant, endpoint)
            # Not an id, so no target: the trail keeps ids only.
            assert (row.result, row.target_id, row.meta["code"]) == (
                "error", None, "not_found"), endpoint


class TestAWriteNamesWhatItMade:
    def test_a_batch_names_its_job(self, client, db, tenant, head):
        r = client.post("/v1/batch", json={"memories": [{"content": "a note", "user_id": "u1"}]},
                        headers=head)
        assert r.status_code == 202, r.text
        (row,) = _rows(db, tenant, "batch:create")
        assert (row.result, row.target_id) == ("ok", r.json()["id"])

    def test_a_declared_name_names_its_alias(self, client, db, tenant, head):
        from korely_agent.db.models import AgentEntityAlias

        r = client.post("/v1/users/u1/names", json={"name": "Maria Rossi"}, headers=head)
        assert r.status_code == 201, r.text
        (row,) = _rows(db, tenant, "names:declare")
        (alias,) = db.query(AgentEntityAlias).filter(AgentEntityAlias.tenant_id == tenant.id).all()
        assert (row.result, row.end_user_id, row.target_id) == ("ok", "u1", str(alias.id))


class TestAReadSaysWhatItShowed:
    def test_the_events_name_the_end_user_and_the_memories(self, client, db, tenant, head):
        mid = _memory(client, head)
        assert client.get("/v1/events", params={"user_id": "u1"}, headers=head).status_code == 200
        r = client.get("/v1/audit", params={"user_id": "u1", "action": "read"}, headers=head)
        (row,) = [e for e in r.json()["events"] if e["meta"]["endpoint"] == "events:list"]
        assert row["user_id"] == "u1" and row["read"]["memories"] == [mid]

    def test_the_users_listing_counts_and_keeps_no_id(self, client, db, tenant, head):
        _memory(client, head, "u1")
        _memory(client, head, "u2")
        assert client.get("/v1/users", params={"limit": 1}, headers=head).status_code == 200
        (row,) = _rows(db, tenant, "users:list")
        assert (row.meta["rows"], row.meta["total"]) == (1, 2)
        assert row.end_user_id is None and row.read_ids is None


class TestPingHasTheCeiling:
    @pytest.fixture
    def two_a_minute(self, monkeypatch):
        from korely_agent.config import get_settings

        monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def test_past_the_ceiling_ping_is_a_429_and_writes_nothing(self, client, db, tenant, head,
                                                                two_a_minute):
        answers = [client.get("/v1/ping", headers=head) for _ in range(5)]
        assert [a.status_code for a in answers] == [200, 200, 429, 429, 429]
        assert answers[2].json()["code"] == "rate_limit_exceeded"
        assert answers[2].headers["Retry-After"]
        assert len(_rows(db, tenant, "ping:get")) == 2

    def test_ping_and_the_other_routes_share_the_counter(self, client, db, tenant, head,
                                                         two_a_minute):
        assert client.get("/v1/users", headers=head).status_code == 200
        assert client.get("/v1/ping", headers=head).status_code == 200
        assert client.get("/v1/users", headers=head).status_code == 429
