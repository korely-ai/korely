"""What the audit of the public SDK found in /v1, ported from the hosted product.

The hosted service fixed these on 2026-09-28 and this
install had every one of them, because the two share the contract:

1. a `user_id` / `agent_id` holding a '/' could be written and never erased:
   `DELETE /v1/users/{end_user}/memories` split the id on the slash and matched
   nothing, which for the GDPR Art. 17 endpoint means the person can never be
   erased, and `DELETE /v1/agents/{agent_id}` could never reach that namespace;
2. a batch item could not carry the `timestamp` a single write takes, so a
   history import landed every fact at the date of the import;
3. `/v1/events` filtered by status after the LIMIT, and counted `processing`
   among the newest hundred rows only;
4. the erasure answer named deleted rows "forgotten" and "invalidated";
5. the MCP read a naive `as_of` as naive, where REST reads it as UTC;
6. the published document described every error as FastAPI's validation list
   and declared nothing but 422, while the wire sends `{"detail": ...}` with a
   string, a `{code, message}` or that list in it.
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from tests.conftest import seeded_project_id
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import keys as key_service
from korely_agent.services.sentinels import EVERY_PROJECT


# ── helpers ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(db, monkeypatch):
    """The app with the worker really off.

    The settings are cached for the process, so RUN_WORKER=false set after the
    first test built them changes nothing and the worker starts with every
    TestClient: its first drain would race the pending rows these tests seed.
    """
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
def spec(db, monkeypatch):
    """The published document. Building the app reads the settings, which
    look for a provider key in the database, hence `db`."""
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


def _v1_operations(doc):
    for path, item in doc["paths"].items():
        if not path.startswith("/v1"):
            continue
        for method, op in item.items():
            yield path, method, op


def _schema(op, status_code):
    return op["responses"][status_code]["content"]["application/json"]["schema"]


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


# ── 1. an id is one path segment, and every stored id can be erased ────────

_UNADDRESSABLE = ["acme/42", "", ".", "..", "a\x00b", "line\nbreak", "x" * 256]
# Ids in other scripts, with emoji and with marks: the rule refuses by
# category what cannot be read back (control characters, line breaks), never
# a letter of another alphabet (Lo), a symbol (So), a mark (Mn, Mc) or a
# joiner (Cf) (the skeptic's mutants of 9 Nov 2026 that refused those lived).
_EVERY_SCRIPT = ["\u30b5\u30dd\u30fc\u30c8\u4fc2", "\u0645\u0633\u0627\u0639\u062f",
                 "\ud55c\uad6d", "\u092c\u0949\u091f", "\u0915\u094d\u0937", "e\u0301",
                 "\U0001F916", "\u2764\ufe0f", "\U0001F469\u200d\U0001F4BB",
                 "\U0001F1EE\U0001F1F9"]


class TestTheRule:
    @pytest.mark.parametrize("bad", _UNADDRESSABLE)
    def test_what_cannot_be_addressed(self, bad):
        from korely_agent.services.ids import namespace_id_problem

        assert namespace_id_problem(bad)

    @pytest.mark.parametrize("good", ["anna", "anna@example.com", "user 42", "Zoë",
                                      "a:b", "%2F", "a.b", "...", "x" * 255] + _EVERY_SCRIPT)
    def test_ordinary_ids_pass(self, good):
        from korely_agent.services.ids import namespace_id_problem

        assert namespace_id_problem(good) is None


class TestNamespaceIds:
    @pytest.mark.parametrize("field", ["user_id", "agent_id"])
    @pytest.mark.parametrize("bad", ["acme/42", "..", ""])
    def test_refused_on_every_rest_write(self, client, head, db, field, bad):
        for path, payload in (
            ("/v1/memories", {"content": "x", field: bad}),
            ("/v1/facts", {"subject": "a", "predicate": "likes", "object": "b", field: bad}),
            ("/v1/batch", {"memories": [{"content": "x", field: bad}]}),
        ):
            r = client.post(path, json=payload, headers=head)
            assert r.status_code == 422, (path, r.text)
            issue = r.json()["detail"][0]
            assert issue["loc"][-1] == field, (path, issue)
            assert issue["type"] == "invalid_namespace_id", (path, issue)
        assert db.query(AgentMemory).count() == 0
        assert db.query(AgentFact).count() == 0

    @pytest.mark.parametrize("field", ["user_id", "agent_id"])
    @pytest.mark.parametrize("good", _EVERY_SCRIPT[::3], ids=["katakana", "devanagari", "emoji",
                                                              "flag"])
    def test_an_id_of_any_script_is_written(self, client, head, field, good):
        for path, payload in (
            ("/v1/memories", {"content": "x", field: good}),
            ("/v1/facts", {"subject": "a", "predicate": "likes", "object": "b", field: good}),
        ):
            r = client.post(path, json=payload, headers=head)
            assert r.status_code in (200, 201, 202), (path, r.text)

    def test_the_rule_is_in_the_published_field(self, spec):
        from korely_agent.services.ids import NAMESPACE_ID_RULE

        s = spec["components"]["schemas"]
        for model in ("AddBody", "FactBody", "BatchMemory"):
            for field in ("user_id", "agent_id"):
                assert NAMESPACE_ID_RULE in s[model]["properties"][field]["description"], \
                    (model, field)

    def test_run_id_is_not_addressed_by_any_path_and_stays_free(self, client, head):
        r = client.post("/v1/memories", json={"content": "x", "user_id": "u", "run_id": "session/7"},
                        headers=head)
        assert r.status_code == 201, r.text

    def test_refused_on_the_mcp_write(self, db, tenant):
        out = _run_tool(tenant, "korely_add", content="x", user_id="acme/42")
        assert out.startswith("error: invalid_request, user_id"), out
        out = _run_tool(tenant, "korely_add", content="x", agent_id="..")
        assert out.startswith("error: invalid_request, agent_id"), out
        assert db.query(AgentMemory).count() == 0

    def test_refused_by_the_batch_worker_too(self, db, tenant, monkeypatch):
        """A job queued before the rule, or by a caller that is not the API,
        still goes through the same rule when its rows are written."""
        from korely_agent.services import batch

        monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                            lambda *a, **k: [])
        job = batch.enqueue_batch(db, tenant.id, [
            {"content": "fine", "user_id": "u1"},
            {"content": "no", "user_id": "acme/42"},
        ])
        batch.process_batch_jobs(db, limit=5)
        got = batch.job_status(db, tenant.id, job.id)
        assert (got["imported"], got["failed"]) == (1, 1)
        assert got["errors"][0]["index"] == 1
        assert "user_id" in got["errors"][0]["error"]
        assert {m.end_user_id for m in db.query(AgentMemory).all()} == {"u1"}

    def test_an_end_user_written_with_a_slash_before_the_rule_can_be_erased(
            self, client, head, db, tenant):
        from korely_agent.services.memory_engine import AgentMemoryEngine

        # Legacy data: written straight through the engine, as the API allowed.
        eng = AgentMemoryEngine(db, tenant.id, seeded_project_id(db, tenant.id))
        eng.add("secret", end_user_id="acme/42")
        eng.add("other", end_user_id="acme")
        r = client.delete("/v1/users/acme%2F42/memories", headers=head)
        assert r.status_code == 200, r.text
        assert r.json()["user_id"] == "acme/42"
        assert r.json()["memories_deleted"] == 1
        db.expire_all()
        assert {m.end_user_id for m in db.query(AgentMemory).all()} == {"acme"}

    def test_an_agent_written_with_a_slash_can_be_deleted(self, client, head, db, tenant):
        from korely_agent.services.memory_engine import AgentMemoryEngine
        from korely_agent.services.usage import distinct_agent_ids

        proj = seeded_project_id(db, tenant.id)
        AgentMemoryEngine(db, tenant.id, proj).add("x", agent_id="team/bot")
        r = client.delete("/v1/agents/team%2Fbot", headers=head)
        assert r.status_code == 200, r.text
        assert r.json()["agent_id"] == "team/bot" and r.json()["memories_deleted"] == 1
        assert distinct_agent_ids(db, tenant.id, project_id=proj) == set()

    def test_the_plain_routes_still_work(self, client, head):
        client.post("/v1/memories", json={"content": "x", "user_id": "maria",
                                          "agent_id": "bot"}, headers=head)
        assert client.delete("/v1/agents/bot", headers=head).json()["memories_deleted"] == 1
        client.post("/v1/memories", json={"content": "y", "user_id": "maria"}, headers=head)
        assert client.delete("/v1/users/maria/memories",
                             headers=head).json()["memories_deleted"] == 1
        assert client.delete("/v1/agents/nope", headers=head).status_code == 404

    def test_the_published_paths_are_unchanged(self, spec):
        assert "delete" in spec["paths"]["/v1/users/{end_user}/memories"]
        assert "delete" in spec["paths"]["/v1/agents/{agent_id}"]


# ── 2. a batch item takes the timestamp a single write takes ───────────────

class TestOneParserForTheEventTime:
    @pytest.mark.parametrize("s", ["2026-03-01", "2026-03-01T10:00:00Z",
                                   "2026-03-01T10:00:00", "2026-03-01T10:00:00+02:00",
                                   "garbage", "", None])
    def test_every_write_path_reads_it_the_same_way(self, s):
        # One door for the request paths (REST, batch, MCP), one parser under
        # it; what it parses goes on the row, where the worker reads it
        # (tests/test_v1_contract_sdk_audit_2.py, item 1).
        from fastapi import HTTPException
        from korely_agent.services.memory_write import require_event_time
        from korely_agent.services.facts import parse_event_time

        try:
            got = require_event_time(s)
        except HTTPException:
            got = None
        assert got == parse_event_time(s)

    def test_date_only_is_midnight_utc(self):
        from korely_agent.services.facts import parse_event_time

        assert parse_event_time("2026-03-01") == datetime(2026, 3, 1, tzinfo=timezone.utc)


class TestBatchTimestamp:
    def test_an_item_with_a_timestamp_is_accepted_and_stored_normalized(
            self, client, head, db):
        from korely_agent.db.models import AgentBatchJob

        r = client.post("/v1/batch", json={"memories": [
            {"content": "Maria moved to Berlin.", "user_id": "maria",
             "timestamp": "2026-03-01"},
            {"content": "Maria likes tea.", "user_id": "maria"},
        ]}, headers=head)
        assert r.status_code == 202, r.text
        payload = db.query(AgentBatchJob).one().payload
        assert payload[0]["timestamp"] == "2026-03-01T00:00:00+00:00"
        assert payload[1].get("timestamp") is None

    @pytest.mark.parametrize("ts", ["yesterday", "01/03/2026", "1709294400"])
    def test_an_unreadable_one_is_refused_like_a_single_write(self, client, head, db, ts):
        from korely_agent.db.models import AgentBatchJob

        r = client.post("/v1/batch", json={"memories": [
            {"content": "fine", "user_id": "maria"},
            {"content": "Maria moved to Berlin.", "user_id": "maria", "timestamp": ts},
        ]}, headers=head)
        assert r.status_code == 422, r.text
        assert "memories[1].timestamp" in r.text and "ISO 8601" in r.text
        assert db.query(AgentBatchJob).count() == 0, "queued it anyway"

    def test_a_blank_one_is_absent_like_a_single_write(self, client, head):
        r = client.post("/v1/batch", json={"memories": [
            {"content": "x", "user_id": "maria", "timestamp": "  "}]}, headers=head)
        assert r.status_code == 202, r.text

    def test_its_facts_inherit_it_as_on_a_single_write(self, db, tenant, monkeypatch):
        """The drain extracts what an import writes (WRITE_ASYNC, the
        default, since 2026-10-06) and dates it by the event time on the row,
        as for an asynchronous single write."""
        from korely_agent.services import batch
        from korely_agent.services.facts import service as facts_service

        seen = []

        def spy(db_, tenant_id, *, memory, valid_from=None, **k):
            seen.append((memory.content, valid_from))
            memory.graph_sync_status = "done"
            db_.commit()
            return []

        monkeypatch.setattr(facts_service, "extract_and_persist_facts", spy)
        job = batch.enqueue_batch(db, tenant.id, [
            {"content": "Maria moved to Berlin.", "user_id": "maria",
             "timestamp": "2026-03-01T00:00:00+00:00"},
            {"content": "Maria likes tea.", "user_id": "maria"},
        ])
        batch.process_batch_jobs(db, limit=5)
        db.refresh(job)
        assert job.imported == 2, job.errors
        assert seen == [], "the import extracted inline with WRITE_ASYNC on"
        facts_service.process_pending_memories(db, limit=10)
        assert sorted(seen, key=lambda x: x[0]) == [
            ("Maria likes tea.", None),
            ("Maria moved to Berlin.", datetime(2026, 3, 1, tzinfo=timezone.utc))]

    def test_a_backfill_after_a_failed_extraction_keeps_it(self, db, tenant, monkeypatch):
        """Extraction in the batch is best effort: when it fails the memory is
        left for the drain, and the drain reads the event time from the row,
        as it does for an asynchronous single write."""
        from korely_agent.services import batch
        from korely_agent.services.memory_core.worker import event_key

        monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                            lambda *a, **k: [])
        batch.enqueue_batch(db, tenant.id, [
            {"content": "Maria moved to Berlin.", "user_id": "maria",
             "timestamp": "2026-03-01T00:00:00+00:00"}])
        batch.process_batch_jobs(db, limit=5)
        m = db.query(AgentMemory).one()
        assert event_key(m) == datetime(2026, 3, 1, tzinfo=timezone.utc)


# ── 3. /v1/events filters before the limit and counts all of the backlog ───

class TestEvents:
    def _rows(self, db, tenant, statuses, end_user="u"):
        """Memories oldest first, one per status, created_at one minute apart."""
        proj = seeded_project_id(db, tenant.id)
        t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
        for i, st in enumerate(statuses):
            db.add(AgentMemory(tenant_id=tenant.id, project_id=proj, end_user_id=end_user,
                               content=f"m{i}", graph_sync_status=st,
                               created_at=t0 + timedelta(minutes=i)))
        db.commit()

    def test_the_status_filter_runs_before_the_limit(self, client, head, db, tenant):
        self._rows(db, tenant, ["error", "done", "done", "done"])
        r = client.get("/v1/events?status=error&limit=2", headers=head)
        assert r.status_code == 200, r.text
        assert [e["status"] for e in r.json()["events"]] == ["error"]

    def test_processing_counts_the_whole_backlog(self, client, head, db, tenant):
        # Two in flight, older than 110 finished writes: outside any page.
        self._rows(db, tenant, ["pending", "processing"] + ["done"] * 110)
        body = client.get("/v1/events?limit=5", headers=head).json()
        assert body["processing"] == 2
        assert len(body["events"]) == 5
        assert client.get("/v1/events?user_id=someone-else",
                          headers=head).json()["processing"] == 0
        # Independent of the status filter, as documented.
        assert client.get("/v1/events?status=error",
                          headers=head).json()["processing"] == 2

    def test_the_sql_filter_agrees_with_the_field(self, db, tenant):
        from korely_agent.services.memory_engine import _PUBLIC_STATUS, AgentMemoryEngine

        for i, st in enumerate(_PUBLIC_STATUS):
            db.add(AgentMemory(tenant_id=tenant.id, content=f"m{i}", graph_sync_status=st))
        db.commit()
        eng = AgentMemoryEngine(db, tenant.id)
        for public in set(_PUBLIC_STATUS.values()):
            rows, total = eng.get_all(status=public, limit=50)
            assert {r["status"] for r in rows} == {public}
            assert total == eng.count(status=public) == \
                list(_PUBLIC_STATUS.values()).count(public)


# ── 4. the erasure answer names what happened ──────────────────────────────

class TestErasureNames:
    def test_the_deleted_names_alone(self, client, head):
        client.post("/v1/memories", json={"content": "a", "user_id": "maria"}, headers=head)
        r = client.post("/v1/facts", json={"subject": "Maria", "predicate": "likes",
                                           "object": "tea", "user_id": "maria"}, headers=head)
        assert r.status_code == 201, r.text
        body = client.delete("/v1/users/maria/memories", headers=head).json()
        assert body["memories_deleted"] == 1 and body["facts_deleted"] == 1
        assert body["erasure"] == "permanent"
        # 0.2.0: the first names (`memories_forgotten`, `facts_invalidated`) are gone.
        # The core's names in this order; a part of the product served to
        # the account adds its places' counts after the facts'
        # (services/facts/erasure.parts_deleted): an install of one's own is
        # served none.
        assert list(body) == ["user_id", "memories_deleted", "facts_deleted",
                              "erasure", "audit_id"]

    def test_the_service_says_it_too(self, db, tenant):
        from korely_agent.services import facts

        out = facts.forget_end_user(db, tenant.id, "nobody", project_id=EVERY_PROJECT)
        assert out["memories_deleted"] == 0 and out["facts_deleted"] == 0
        assert "memories_forgotten" not in out and "facts_invalidated" not in out

    def test_the_old_names_are_gone_from_the_spec(self, spec):
        ops = {(p, m): op for p, m, op in _v1_operations(spec)}
        assert _schema(ops[("/v1/users/{end_user}/memories", "delete")], "200") == \
            {"$ref": "#/components/schemas/UserErasureResponse"}
        props = spec["components"]["schemas"]["UserErasureResponse"]["properties"]
        assert "memories_forgotten" not in props and "facts_invalidated" not in props
        assert not props["memories_deleted"].get("deprecated")
        assert not props["facts_deleted"].get("deprecated")


# ── 5. the MCP reads as_of in UTC, as REST does ─────────────────────────────

class TestOneParserForAsOf:
    @pytest.mark.parametrize("s,expected", [
        ("2026-06-01", datetime(2026, 6, 1, tzinfo=timezone.utc)),
        ("2026-06-01T10:00:00", datetime(2026, 6, 1, 10, tzinfo=timezone.utc)),
        ("2026-06-01T10:00:00Z", datetime(2026, 6, 1, 10, tzinfo=timezone.utc)),
    ])
    def test_no_offset_means_utc(self, s, expected):
        from korely_agent.api.facts import parse_as_of
        from korely_agent.services.facts.temporal import parse_moment

        assert parse_moment(s) == expected and parse_moment(s).tzinfo is not None
        assert parse_as_of(s) == parse_moment(s)

    def test_an_offset_is_kept(self):
        from korely_agent.services.facts.temporal import parse_moment

        got = parse_moment("2026-06-01T10:00:00+02:00")
        assert got == datetime(2026, 6, 1, 8, tzinfo=timezone.utc)

    def test_garbage_raises(self):
        from korely_agent.services.facts.temporal import parse_moment

        with pytest.raises(ValueError):
            parse_moment("yesterday")


class TestTheMcpAsOf:
    def test_as_of_without_offset_is_utc_as_on_rest(self, db, tenant, monkeypatch):
        from korely_agent.api.facts import parse_as_of
        from korely_agent.services import facts as agent_facts

        seen = {}

        def spy(*a, **k):
            seen["as_of"] = k.get("as_of")
            return [], 0

        monkeypatch.setattr(agent_facts, "retrieve_agent_facts", spy)
        _run_tool(tenant, "korely_get_facts", as_of="2026-06-01")
        assert seen["as_of"] == datetime(2026, 6, 1, tzinfo=timezone.utc)
        assert seen["as_of"].tzinfo is not None
        assert parse_as_of("2026-06-01") == seen["as_of"]

    def test_a_blank_as_of_is_absent_as_on_rest(self, db, tenant, monkeypatch):
        from korely_agent.services import facts as agent_facts

        seen = {}

        def spy(*a, **k):
            seen["as_of"] = k.get("as_of")
            return [], 0

        monkeypatch.setattr(agent_facts, "retrieve_agent_facts", spy)
        out = _run_tool(tenant, "korely_get_facts", as_of="  ")
        assert seen["as_of"] is None and not out.startswith("error")

    def test_garbage_is_still_refused(self, db, tenant):
        out = _run_tool(tenant, "korely_get_facts", as_of="yesterday")
        # REST's code and sentence (audit of 17 Oct 2026).
        assert out.startswith("error: invalid_request, as_of must be an ISO date")

    def test_the_answer_is_the_same_on_both_doors(self, client, head, db, tenant,
                                                  monkeypatch):
        """A fact that ended at 20:00 UTC on 31 May is over at midnight UTC on
        1 June. Postgres reads a naive value against a timestamptz column in the
        session's time zone, so on a session at UTC+14 the naive midnight the
        MCP used to send was 10:00 UTC on 31 May, and the fact came back."""
        from korely_agent.db import get_sessionmaker
        from sqlalchemy import text

        r = client.post("/v1/facts", json={"subject": "u1", "predicate": "is_on",
                                           "object": "Gold", "user_id": "u1",
                                           "valid_from": "2026-01-01"}, headers=head)
        fid = r.json()["id"]
        r = client.post(f"/v1/facts/{fid}/forget", json={"at": "2026-05-31T20:00:00Z"},
                        headers=head)
        assert r.status_code == 200, r.text
        rest = client.get("/v1/facts?user_id=u1&as_of=2026-06-01", headers=head).json()
        assert rest["facts"] == []

        base = get_sessionmaker()

        def far_east():
            s = base()
            # LOCAL: gone at the end of the transaction, so the pooled
            # connection goes back the way it came.
            s.execute(text("SET LOCAL TIME ZONE 'Pacific/Kiritimati'"))
            return s

        monkeypatch.setattr("korely_agent.mcp.server.get_sessionmaker", lambda: far_east)
        mcp = _run_tool(tenant, "korely_get_facts", user_id="u1", as_of="2026-06-01")
        assert "Gold" not in mcp, mcp


# ── 6. the errors the document declares are the errors the wire sends ──────

class TestTheSpecSaysWhatTheWireSends:
    def test_every_v1_error_is_the_detail_envelope(self, spec):
        # `detail` as always, and `code` and `message` beside it since the
        # second pass (tests/test_v1_contract_sdk_audit_2.py, item 3).
        s = spec["components"]["schemas"]
        assert s["ApiError"]["required"] == ["detail", "code", "message"]
        for path, method, op in _v1_operations(spec):
            for status_code in op["responses"]:
                if status_code.startswith(("4", "5")):
                    assert _schema(op, status_code) == {"$ref": "#/components/schemas/ApiError"}, \
                        (method, path, status_code)

    def test_the_errors_each_route_can_answer_are_declared(self, spec):
        ops = {(p, m): op for p, m, op in _v1_operations(spec)}
        for (path, method), op in ops.items():
            codes = set(op["responses"])
            if path == "/v1/ping":
                # No scope and no parameters; the ceiling since the audit
                # of 18 Oct 2026, and the 503 of a key checked with
                # the database down.
                assert {"401", "429", "500", "503"} <= codes and "422" not in codes, codes
            else:
                assert {"401", "403", "422", "429", "500"} <= codes, (method, path, codes)
        for route, needed in {
            ("/v1/memories/{memory_id}", "get"): {"404"},
            ("/v1/memories/{memory_id}", "patch"): {"404", "409"},
            ("/v1/memories/{memory_id}", "delete"): {"404"},
            ("/v1/memories/{memory_id}/history", "get"): {"404"},
            ("/v1/agents/{agent_id}", "delete"): {"404"},
            ("/v1/facts/{fact_id}/forget", "post"): {"404"},
            ("/v1/facts/{fact_id}", "patch"): {"404"},
            ("/v1/batch/{job_id}", "get"): {"404"},
            ("/v1/memories/search", "post"): {"503"},
            ("/v1/facts", "post"): {"503"},
        }.items():
            assert needed <= set(ops[route]["responses"]), route

    def test_every_shape_the_wire_sends_parses_as_the_declared_model(
            self, client, head, db, tenant):
        from korely_agent.api.errors import ApiError

        answers = [
            # FastAPI's validation list, without `input`.
            client.post("/v1/memories", json={}, headers=head),
            # A string, from an endpoint's own refusal.
            client.post("/v1/memories", json={"content": "   "}, headers=head),
            # {code, message}.
            client.post("/v1/facts/fct_" + uuid.uuid4().hex + "/forget", headers=head),
            # 401 and 403, strings.
            client.get("/v1/memories"),
        ]
        _r, read_only = key_service.create_api_key(db, tenant_id=tenant.id, name="ro",
                                                   scopes=["memories:read"])
        answers.append(client.post("/v1/memories", json={"content": "x"},
                                   headers={"Authorization": f"Bearer {read_only}"}))
        assert [a.status_code for a in answers] == [422, 422, 404, 401, 403]
        for a in answers:
            assert list(a.json()) == ["detail", "code", "message"], a.text
            ApiError.model_validate(a.json())

    def test_a_closed_before_it_began_422_parses_too(self, client, head):
        from korely_agent.api.errors import ApiError

        fid = client.post("/v1/facts", json={"subject": "u1", "predicate": "is_on",
                                             "object": "Gold", "user_id": "u1",
                                             "valid_from": "2026-06-01"},
                          headers=head).json()["id"]
        r = client.post(f"/v1/facts/{fid}/forget", json={"at": "2026-01-01"}, headers=head)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "invalid_request"
        ApiError.model_validate(r.json())
