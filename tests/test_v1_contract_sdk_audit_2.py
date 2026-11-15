"""The second pass of the SDK audit, ported from the hosted product.

The hosted service fixed these on 2026-09-28
and this install had them too, because the two share the contract:

1. the event time of a write (`timestamp`) had no place of its own. The
   asynchronous write and every batch item put it INSIDE the developer's
   `metadata`, as `_event_time`, and it came back on every read; a key of that
   name written by the developer decided the dates of the facts; and a
   synchronous write did not store it at all, so a memory the drain finished
   later (an inline extraction that failed) had its facts dated today. It is a
   column now, `agent_memories.event_time`, stored by every write path and the
   only thing the worker reads;
2. `get_all` cut every page at 100 while `/v1/memories` and `/v1/events`
   accept `limit` up to 200: `limit=200` quietly returned half a page;
3. the hosted API answers errors as `{code, message}` and this install as
   `{"detail": ...}`. Every error body here now carries `code` and `message`
   too, with `detail` exactly as before, so a client written for either
   parses both and nothing that reads `detail` breaks;
4. `/admin` declared FastAPI's validation list for its 422s while several
   answer a sentence: the same envelope is declared there now;
6. the MCP `korely_add` took no `timestamp`, unlike POST /v1/memories.

(5 is in tests/test_the_worker_stays_off.py, 7 in
tests/test_v1_project_wall_audit.py.)
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from tests.conftest import seeded_project_id
from korely_agent.db.models import AgentMemory
from korely_agent.services import keys as key_service



# ── helpers ───────────────────────────────────────────────────────────────

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


@pytest.fixture
def admin(db, tenant):
    _row, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="admin",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    return {"Authorization": f"Bearer {full}"}


@pytest.fixture
def spec(db, monkeypatch):
    from korely_agent.main import create_app
    from korely_agent.mcp import server as mcp_server

    monkeypatch.setenv("RUN_WORKER", "false")
    mcp_server._agent_mcp_singleton = None
    mcp_server._agent_streamable_app_singleton = None
    try:
        yield json.loads(json.dumps(create_app().openapi()))
    finally:
        mcp_server._agent_mcp_singleton = None
        mcp_server._agent_streamable_app_singleton = None


@pytest.fixture
def no_model(monkeypatch):
    """No embedding and an inline extraction that fails, which leaves the
    memory pending for the drain, as a provider outage does."""
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                        lambda *a, **k: [])


@pytest.fixture
def drain(monkeypatch, db):
    """Run the worker's drain and return the valid_from it hands extraction,
    per memory content."""
    from korely_agent.services.facts import service

    seen = {}

    def spy(db_, tenant_id, *, memory, valid_from=None, **k):
        seen[memory.content] = valid_from
        memory.graph_sync_status = "done"
        db_.commit()
        return []

    def run():
        monkeypatch.setattr(service, "extract_and_persist_facts", spy)
        service.process_pending_memories(db, limit=50)
        return seen

    return run


def _sync_writes(monkeypatch):
    # Through the real settings, which every door reads (the write is
    # services/memory_write.py's since the audit of 17 Oct 2026).
    from korely_agent.config import get_settings

    monkeypatch.setenv("WRITE_ASYNC", "false")
    get_settings.cache_clear()


def _async_writes(monkeypatch):
    # Through the real settings, which every door reads (the write is
    # services/memory_write.py's since the audit of 17 Oct 2026).
    from korely_agent.config import get_settings

    monkeypatch.setenv("WRITE_ASYNC", "true")
    get_settings.cache_clear()


def _write_async(monkeypatch, on):
    """WRITE_ASYNC for the MCP door, through the real settings: the MCP server
    is built afresh for each test (conftest.py) and reads them as it is."""
    from korely_agent.config import get_settings

    monkeypatch.setenv("WRITE_ASYNC", "true" if on else "false")
    get_settings.cache_clear()


def _utc(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


MARCH = datetime(2026, 3, 1, tzinfo=timezone.utc)


# ── 1. the event time is a column, not a key in the developer's metadata ──

class TestTheEventTimeHasAColumn:
    def test_a_synchronous_write_keeps_it_for_the_drain(
            self, client, head, db, no_model, drain, monkeypatch):
        """WRITE_ASYNC=false and the inline extraction fails: the memory is
        left pending, and the drain that finishes it dates its facts on the
        day the write asked for, not on the day of the drain."""
        _sync_writes(monkeypatch)
        r = client.post("/v1/memories", headers=head, json={
            "content": "Maria moved to Berlin.", "user_id": "maria",
            "timestamp": "2026-03-01"})
        assert r.status_code == 201, r.text
        row = db.query(AgentMemory).one()
        assert row.graph_sync_status == "pending"
        assert drain() == {"Maria moved to Berlin.": MARCH}, (
            "the drain dated the facts of a synchronous write on the day it ran")

    def test_an_asynchronous_write_leaves_the_metadata_alone(
            self, client, head, db, no_model, drain, monkeypatch):
        _async_writes(monkeypatch)
        r = client.post("/v1/memories", headers=head, json={
            "content": "Maria moved to Berlin.", "user_id": "maria",
            "metadata": {"tier": "pro"}, "timestamp": "2026-03-01T00:00:00Z"})
        assert r.status_code == 201, r.text
        mid = r.json()["id"]
        assert r.json()["metadata"] == {"tier": "pro"}
        assert client.get(f"/v1/memories/{mid}", headers=head).json()["metadata"] == {"tier": "pro"}
        listed = client.get("/v1/memories", headers=head).json()["memories"]
        assert [m["metadata"] for m in listed] == [{"tier": "pro"}]
        row = db.query(AgentMemory).one()
        assert row.meta == {"tier": "pro"}
        assert drain() == {"Maria moved to Berlin.": MARCH}

    def test_no_metadata_stays_no_metadata(self, client, head, db, no_model, monkeypatch):
        _async_writes(monkeypatch)
        r = client.post("/v1/memories", headers=head, json={
            "content": "Maria moved to Berlin.", "user_id": "maria",
            "timestamp": "2026-03-01"})
        assert r.json()["metadata"] == {}
        assert db.query(AgentMemory).one().meta in (None, {})

    def test_a_key_of_that_name_in_the_metadata_dates_nothing(
            self, client, head, db, no_model, drain, monkeypatch):
        """`_event_time` in the developer's own metadata is their data: it is
        returned as they wrote it and it does not decide when facts began."""
        _async_writes(monkeypatch)
        r = client.post("/v1/memories", headers=head, json={
            "content": "Maria likes tea.", "user_id": "maria",
            "metadata": {"_event_time": "2020-01-01"}})
        assert r.status_code == 201, r.text
        assert r.json()["metadata"] == {"_event_time": "2020-01-01"}
        assert drain() == {"Maria likes tea.": None}

    def test_every_write_path_stores_it_on_the_row(
            self, client, head, db, no_model, monkeypatch):
        from korely_agent.services import batch

        for mode in (_sync_writes, _async_writes):
            mode(monkeypatch)
            client.post("/v1/memories", headers=head, json={
                "content": f"written {mode.__name__}", "user_id": "maria",
                "timestamp": "2026-03-01T09:30:00+02:00"})
        r = client.post("/v1/batch", headers=head, json={"memories": [
            {"content": "imported", "user_id": "maria", "timestamp": "2026-03-01T09:30:00+02:00"}]})
        assert r.status_code == 202, r.text
        batch.process_batch_jobs(db, limit=5)
        rows = db.query(AgentMemory).all()
        assert len(rows) == 3
        for row in rows:
            db.refresh(row)
            assert _utc(row.event_time) == datetime(2026, 3, 1, 7, 30, tzinfo=timezone.utc), row.content
            assert "_event_time" not in (row.meta or {}), row.content

    def test_a_batch_item_the_drain_finishes_keeps_it(self, db, tenant, no_model, drain):
        from korely_agent.services import batch

        batch.enqueue_batch(db, tenant.id, [
            {"content": "Maria moved to Berlin.", "user_id": "maria",
             "metadata": {"source": "crm"}, "timestamp": "2026-03-01T00:00:00+00:00"}],
            project_id=seeded_project_id(db, tenant.id))
        batch.process_batch_jobs(db, limit=5)
        row = db.query(AgentMemory).one()
        assert row.meta == {"source": "crm"}
        assert drain() == {"Maria moved to Berlin.": MARCH}

    def test_the_sort_key_reads_the_column(self):
        from korely_agent.services.memory_core.worker import event_key

        m = AgentMemory(content="x", event_time=MARCH, meta={"_event_time": "2020-01-01"},
                        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
        assert event_key(m) == MARCH
        m = AgentMemory(content="x", meta={"_event_time": "2020-01-01"},
                        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
        assert event_key(m) == datetime(2026, 9, 1, tzinfo=timezone.utc)


# ── 2. a page of 200 is a page of 200 ─────────────────────────────────────

class TestAPageOf200:
    def _seed(self, db, tenant, n):
        proj = seeded_project_id(db, tenant.id)
        t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
        db.add_all([AgentMemory(tenant_id=tenant.id, project_id=proj, end_user_id="u",
                                content=f"m{i}", created_at=t0 + timedelta(seconds=i))
                    for i in range(n)])
        db.commit()

    def test_memories_and_events_return_what_they_accept(self, client, head, db, tenant):
        self._seed(db, tenant, 150)
        page = client.get("/v1/memories", headers=head, params={"limit": 200}).json()
        assert page["total"] == 150 and len(page["memories"]) == 150
        events = client.get("/v1/events", headers=head, params={"limit": 200}).json()
        assert len(events["events"]) == 150

    def test_the_engine_and_the_routes_share_one_number(self, spec):
        from korely_agent.services.memory_engine import MAX_PAGE

        assert MAX_PAGE == 200
        for path in ("/v1/memories", "/v1/events"):
            (limit,) = [p for p in spec["paths"][path]["get"]["parameters"] if p["name"] == "limit"]
            assert limit["schema"]["maximum"] == MAX_PAGE, path


# ── 3. every error carries code and message, and detail as before ─────────

class TestTheErrorEnvelope:
    def _answers(self, client, head, db, tenant):
        _r, read_only = key_service.create_api_key(db, tenant_id=tenant.id, name="ro",
                                                   scopes=["memories:read"])
        return {
            "422 list": client.post("/v1/memories", json={}, headers=head),
            "422 sentence": client.post("/v1/memories", json={"content": "   "}, headers=head),
            "422 code": client.post("/v1/memories", headers=head, json={
                "content": "x", "timestamp": "yesterday"}),
            "404 sentence": client.get("/v1/memories/mem_" + uuid.uuid4().hex, headers=head),
            "404 code": client.post("/v1/facts/fct_" + uuid.uuid4().hex + "/forget", headers=head),
            "401": client.get("/v1/memories"),
            "403": client.post("/v1/memories", json={"content": "x"},
                               headers={"Authorization": f"Bearer {read_only}"}),
        }

    def test_the_real_answers(self, client, head, db, tenant):
        from korely_agent.api.errors import ApiError

        got = self._answers(client, head, db, tenant)
        assert {k: r.status_code for k, r in got.items()} == {
            "422 list": 422, "422 sentence": 422, "422 code": 422, "404 sentence": 404,
            "404 code": 404, "401": 401, "403": 403}
        for name, r in got.items():
            body = r.json()
            assert set(body) == {"detail", "code", "message"}, (name, body)
            assert isinstance(body["code"], str) and body["code"], name
            assert isinstance(body["message"], str) and body["message"], name
            ApiError.model_validate(body)
        assert {k: r.json()["code"] for k, r in got.items()} == {
            "422 list": "invalid_request", "422 sentence": "invalid_request",
            "422 code": "invalid_request", "404 sentence": "not_found",
            "404 code": "not_found", "401": "invalid_key", "403": "forbidden"}

    def test_detail_is_exactly_what_it_was(self, client, head, db, tenant):
        got = self._answers(client, head, db, tenant)
        issues = got["422 list"].json()["detail"]
        assert isinstance(issues, list) and issues[0]["loc"] == ["body", "content"]
        assert "input" not in issues[0]
        assert got["422 sentence"].json()["detail"] == "content must not be empty or whitespace"
        assert got["404 sentence"].json()["detail"] == "Memory not found"
        assert got["404 code"].json()["detail"] == {"code": "not_found",
                                                    "message": "No fact with that id."}
        assert got["401"].json()["detail"] == ("Invalid or missing API key: send it in the header "
                                               "Authorization: Bearer kor_self_...")
        assert got["403"].json()["detail"] == "API key missing required scope(s): memories:write"

    def test_message_is_the_sentence_or_the_code_message(self, client, head, db, tenant):
        got = self._answers(client, head, db, tenant)
        for name in ("422 sentence", "404 sentence", "401", "403"):
            body = got[name].json()
            assert body["message"] == body["detail"], name
        body = got["404 code"].json()
        assert (body["code"], body["message"]) == (body["detail"]["code"], body["detail"]["message"])
        assert got["422 list"].json()["message"] == "content: Field required"

    def test_the_422_message_never_repeats_what_was_sent(self, client, admin):
        secret = "AQ.Ab8RN6-this-must-not-come-back"
        r = client.put("/admin/provider", headers=admin, json={"api_key": secret})
        assert r.status_code == 422
        assert secret not in r.text
        assert r.json()["code"] == "invalid_request"
        assert r.json()["message"] == "key: Field required (and 1 more)"

    def test_the_422_message_keeps_the_did_you_mean(self, client, head):
        r = client.post("/v1/memories", headers=head,
                        json={"content": "x", "userId": "anna"})
        assert r.status_code == 422
        assert r.json()["message"] == "userId is not a field here. Did you mean user_id?"

    def test_a_route_that_does_not_exist_answers_the_envelope_too(self, client, head):
        r = client.post("/v1/nothing-here", headers=head, json={})
        assert r.status_code in (404, 405)
        assert {"detail", "code", "message"} <= set(r.json())

    def test_the_rate_limit_answers_it(self, client, head, monkeypatch):
        from korely_agent import config

        monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "1")
        config.get_settings.cache_clear()
        client.get("/v1/memories", headers=head)
        r = client.get("/v1/memories", headers=head)
        assert r.status_code == 429, r.text
        assert r.json()["code"] == "rate_limit_exceeded"
        assert r.json()["message"] == r.json()["detail"]
        assert r.headers.get("Retry-After")

    def test_a_failure_of_ours_answers_it(self, db, monkeypatch):
        from fastapi.testclient import TestClient
        from korely_agent.db import get_db
        from korely_agent.main import create_app

        app = create_app()
        app.dependency_overrides[get_db] = lambda: db

        @app.get("/v1/_boom_for_the_test")
        def boom():
            raise RuntimeError("nothing to see")

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/v1/_boom_for_the_test")
        assert r.status_code == 500
        assert r.json() == {"detail": "internal error", "code": "internal_error",
                            "message": "internal error"}

    def test_the_spec_declares_all_three(self, spec):
        s = spec["components"]["schemas"]["ApiError"]
        assert set(s["required"]) == {"detail", "code", "message"}
        assert s["properties"]["code"]["type"] == "string"
        assert s["properties"]["message"]["type"] == "string"


# ── 4. /admin declares the shape it sends ────────────────────────────────

class TestAdminDeclaresTheEnvelope:
    def test_every_admin_error_is_the_envelope(self, spec):
        wrong = []
        for path, item in spec["paths"].items():
            if not path.startswith("/admin"):
                continue
            for method, op in item.items():
                for code, resp in op.get("responses", {}).items():
                    if code.startswith(("4", "5")):
                        schema = resp["content"]["application/json"]["schema"]
                        if schema != {"$ref": "#/components/schemas/ApiError"}:
                            wrong.append((method, path, code, schema))
        assert wrong == [], wrong

    def test_nothing_in_the_document_declares_the_bare_validation_list(self, spec):
        assert "HTTPValidationError" not in json.dumps(spec["paths"])

    def test_a_route_that_answers_a_sentence_declares_its_422(self, spec):
        op = spec["paths"]["/admin/facts/{fact_id}"]["patch"]
        assert op["responses"]["422"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ApiError"}

    def test_the_real_admin_answers(self, client, admin, db, tenant):
        from korely_agent.api.errors import ApiError
        from korely_agent.services import operators as operator_service

        sentence = client.patch("/admin/facts/fct_" + uuid.uuid4().hex, headers=admin, json={})
        assert sentence.status_code == 422
        assert sentence.json()["detail"] == "give at least one of subject, predicate or object"
        assert sentence.json()["code"] == "invalid_request"
        client.post("/admin/projects", headers=admin, json={"name": "Twice"})
        dup = client.post("/admin/projects", headers=admin, json={"name": "Twice"})
        # The projects routes name the case, as the hosted service does
        # (agents_projects.py) and as the screen expects: the envelope is the
        # same, `detail` is the {code, message} pair the README lists as one of
        # its three shapes. Until 0.1.15 this was the generic `conflict` and
        # the screen showed "Something went wrong" (round 13).
        assert dup.status_code == 409 and dup.json()["code"] == "project_name_taken"
        assert dup.json()["detail"] == {"code": "project_name_taken",
                                        "message": "a project with that name already exists"}
        assert dup.json()["message"] == "a project with that name already exists"
        operator_service.create(db, tenant_id=tenant.id, username="op")
        wrong = client.post("/admin/session", json={"username": "op", "password": "wrong"})
        assert wrong.status_code == 401
        assert wrong.json()["code"] == "invalid_credentials"
        assert wrong.json()["detail"] == "Wrong username or password"
        for r in (sentence, dup, wrong):
            ApiError.model_validate(r.json())


# ── 6. the MCP korely_add takes a timestamp ──────────────────────────────

def _run_tool(tenant, name, **kwargs):
    """Call one MCP tool as a key of this tenant's default project would."""
    from korely_agent.db import get_sessionmaker
    from korely_agent.mcp import server as mcp_server

    s = get_sessionmaker()()
    try:
        proj = seeded_project_id(s, tenant.id)
        s.commit()
    finally:
        s.close()

    async def go():
        mcp_server._agent_user_id.set(str(tenant.id))
        mcp_server._agent_project.set(str(proj))
        mcp_server._agent_scopes.set(("memories:read", "memories:write"))
        fn = mcp_server._get_agent_mcp()._tool_manager._tools[name].fn
        try:
            return await fn(**kwargs)
        except ToolError as e:
            # A refusal is an MCP error since 2026-10-06 (isError); the
            # agent reads the same words.
            return str(e)

    return asyncio.run(go())


class TestMcpAddTakesTheTimestamp:
    def test_the_tool_declares_it(self):
        from korely_agent.mcp import server as mcp_server

        params = mcp_server._get_agent_mcp()._tool_manager._tools["korely_add"].parameters
        assert "timestamp" in params["properties"]

    def test_stored_on_the_row_and_read_by_the_drain(self, db, tenant, no_model, drain,
                                                     monkeypatch):
        _write_async(monkeypatch, True)
        out = _run_tool(tenant, "korely_add", content="Maria moved to Berlin.",
                        user_id="maria", timestamp="2026-03-01T09:30:00Z")
        assert out.startswith("Stored mem_"), out
        row = db.query(AgentMemory).one()
        assert _utc(row.event_time) == datetime(2026, 3, 1, 9, 30, tzinfo=timezone.utc)
        assert row.meta is None
        assert drain() == {"Maria moved to Berlin.":
                           datetime(2026, 3, 1, 9, 30, tzinfo=timezone.utc)}

    def test_a_synchronous_write_hands_it_to_the_extraction(self, db, tenant, monkeypatch):
        from korely_agent.mcp import server as mcp_server

        seen = []
        monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
        _write_async(monkeypatch, False)
        monkeypatch.setattr(mcp_server.agent_facts, "extract_and_persist_facts",
                            lambda db_, t, *, memory, valid_from=None, **k: seen.append(valid_from) or [])
        _run_tool(tenant, "korely_add", content="Maria moved to Berlin.",
                  user_id="maria", timestamp="2026-03-01")
        assert seen == [MARCH]

    def test_an_unreadable_one_is_refused_with_the_rest_words(self, db, tenant):
        out = _run_tool(tenant, "korely_add", content="x", timestamp="next tuesday")
        assert out == ("error: invalid_request, timestamp 'next tuesday' is not an ISO 8601 "
                       "date or datetime (e.g. '2026-03-01' or '2026-03-01T14:30:00Z')")
        assert db.query(AgentMemory).count() == 0

    def test_a_blank_one_is_absent(self, db, tenant, no_model):
        out = _run_tool(tenant, "korely_add", content="x", user_id="u", timestamp="  ")
        assert out.startswith("Stored mem_"), out
        assert db.query(AgentMemory).one().event_time is None

