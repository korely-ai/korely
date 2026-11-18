"""An agent can correct a wrong fact and forget a wrong memory over MCP.

Until this file the MCP door held four tools (add, search, get_facts,
get_context): an agent could add and read, and only REST could fix or retract.
The users of other memory products ask for exactly this ("the agent finds the
stale entry but cannot delete or fix it"). `korely_correct_fact` and
`korely_forget_memory` are the twins of PATCH /v1/facts/{id} (and POST
/v1/facts/{id}/forget) and DELETE /v1/memories/{id}, through the same
service functions: this file asks the two doors the same questions.
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from korely_agent.db.models import AgentFact, AgentMemory, AgentProject, AuditEvent
from korely_agent.mcp import server as mcp_server
from korely_agent.services import keys as key_service

READ, WRITE = "memories:read", "memories:write"


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                        lambda *a, **k: [])


@pytest.fixture
def client(db, monkeypatch):
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


def _key(db, tenant, project, scopes=(READ, WRITE), users=None):
    _row, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name=f"k_{uuid.uuid4().hex[:6]}", project_id=project.id,
        scopes=list(scopes), end_users=users)
    return full


def tool(key, name, **args):
    """(refused, text) of one call through the server's own dispatch, scope
    check and argument check included, after verifying `key` as the transport
    does."""
    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(key) is not None
        try:
            out = await mcp_server._get_agent_mcp().call_tool(name, args)
        except ToolError as e:
            return True, str(e)
        content = out[0] if isinstance(out, tuple) else out
        return False, "".join(c.text for c in content)
    return asyncio.run(go())


@pytest.fixture
def world(client, db, tenant):
    """Project Alfa with a memory and a fact; project Beta empty; one key each."""
    a = _project(db, tenant, "Alfa", default=True)
    b = _project(db, tenant, "Beta")
    ka, kb = _key(db, tenant, a), _key(db, tenant, b)
    head = {"Authorization": f"Bearer {ka}"}
    mem = client.post("/v1/memories", headers=head, json={
        "content": "Anna lives in Rome.", "user_id": "anna"}).json()["id"]
    fact = client.post("/v1/facts", headers=head, json={
        "subject": "anna", "predicate": "lives_in", "object": "Rome", "user_id": "anna"}
    ).json()["id"]
    return {"a": a, "b": b, "ka": ka, "kb": kb, "head": head, "mem": mem, "fact": fact}


def trail(db, tenant, endpoint):
    db.expire_all()
    rows = db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id).order_by(AuditEvent.ts)
    return [r for r in rows if (r.meta or {}).get("endpoint") == endpoint]


def facts_of(client, head, **params):
    return client.get("/v1/facts", headers=head, params={"user_id": "anna", **params}
                      ).json()["facts"]


class TestTheToolsAreRegistered:
    def test_both_are_listed_with_their_scope(self):
        names = {t.name for t in mcp_server._get_agent_mcp()._tool_manager.list_tools()}
        assert {"korely_correct_fact", "korely_forget_memory"} <= names
        assert mcp_server.TOOL_SCOPES["korely_correct_fact"] == WRITE
        assert mcp_server.TOOL_SCOPES["korely_forget_memory"] == WRITE


class TestCorrectAFact:
    def test_a_correction_closes_the_old_fact_and_writes_a_new_one(self, client, db, world):
        refused, text = tool(world["ka"], "korely_correct_fact",
                             fact_id=world["fact"], object="Milan")
        assert not refused, text
        assert "Corrected" in text and world["fact"] in text, text
        current = facts_of(client, world["head"])
        assert [f["object"] for f in current] == ["Milan"]
        assert current[0]["id"] != world["fact"], "a fact was edited in place"

    def test_history_keeps_the_closed_fact(self, client, world):
        from datetime import datetime, timedelta, timezone

        between = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        import time
        time.sleep(1.2)
        tool(world["ka"], "korely_correct_fact", fact_id=world["fact"], object="Milan")
        every = facts_of(client, world["head"], include_invalidated="true")
        by_id = {f["id"]: f for f in every}
        assert by_id[world["fact"]]["object"] == "Rome"
        assert by_id[world["fact"]]["invalid_at"] is not None
        # as_of between the write and the correction still says Rome.
        then = facts_of(client, world["head"], as_of=between)
        assert [f["object"] for f in then] == ["Rome"]
        assert [f["object"] for f in facts_of(client, world["head"])] == ["Milan"]
        out = tool(world["ka"], "korely_get_facts", user_id="anna", include_invalidated=True)[1]
        assert "Rome" in out and "Milan" in out

    def test_close_ends_a_fact_and_keeps_it_in_history(self, client, world):
        refused, text = tool(world["ka"], "korely_correct_fact",
                             fact_id=world["fact"], close=True)
        assert not refused, text
        assert text.startswith("forgotten") and world["fact"] in text, text
        assert facts_of(client, world["head"]) == []
        assert world["fact"] in [f["id"] for f in facts_of(
            client, world["head"], include_invalidated="true")]
        again = tool(world["ka"], "korely_correct_fact", fact_id=world["fact"], close=True)[1]
        assert again.startswith("already_forgotten"), again

    def test_a_fact_that_is_history_is_refused_as_rest_refuses_it(self, client, world):
        tool(world["ka"], "korely_correct_fact", fact_id=world["fact"], object="Milan")
        r = client.patch(f"/v1/facts/{world['fact']}", headers=world["head"],
                         json={"object": "Turin"})
        assert r.status_code == 409
        refused, text = tool(world["ka"], "korely_correct_fact",
                             fact_id=world["fact"], object="Turin")
        assert refused and "fact_not_current" in text, text
        assert r.json()["message"] in text

    @pytest.mark.parametrize("args", [
        {}, {"close": True, "object": "Milan"}, {"at": "2026-01-01", "object": "Milan"},
        {"object": ""}, {"object": "x" * 100_000}])
    def test_a_malformed_call_is_refused_before_anything_runs(self, db, tenant, world, args):
        refused, text = tool(world["ka"], "korely_correct_fact", fact_id=world["fact"], **args)
        assert refused and "error: invalid_request" in text, text
        assert facts_of_db(db, world["fact"]).invalid_at is None
        assert trail(db, tenant, "facts:correct") == []

    def test_a_read_only_key_is_refused_and_changes_nothing(self, db, tenant, world):
        key = _key(db, tenant, world["a"], scopes=[READ])
        refused, text = tool(key, "korely_correct_fact", fact_id=world["fact"], object="Milan")
        assert refused and "memories:write" in text, text
        assert facts_of_db(db, world["fact"]).invalid_at is None

    def test_an_unknown_id_is_not_found(self, db, tenant, world):
        for fid in ("fct_" + uuid.uuid4().hex, "not-an-id"):
            refused, text = tool(world["ka"], "korely_correct_fact", fact_id=fid, object="x")
            assert refused and "error: not_found" in text, text
        rows = trail(db, tenant, "facts:correct")
        assert [(r.result, r.meta["code"]) for r in rows] == [("error", "not_found")] * 2

    def test_another_projects_fact_is_not_found_and_untouched(self, client, db, tenant, world):
        rest = client.patch(f"/v1/facts/{world['fact']}", json={"object": "Milan"},
                            headers={"Authorization": f"Bearer {world['kb']}"})
        assert rest.status_code == 404
        refused, text = tool(world["kb"], "korely_correct_fact",
                             fact_id=world["fact"], object="Milan")
        assert refused and "error: not_found" in text, text
        refused, text = tool(world["kb"], "korely_correct_fact", fact_id=world["fact"], close=True)
        assert refused and "error: not_found" in text, text
        assert facts_of_db(db, world["fact"]).invalid_at is None
        assert [f["object"] for f in facts_of(client, world["head"])] == ["Rome"]

    def test_a_key_confined_to_other_end_users_does_not_find_it(self, db, tenant, world):
        key = _key(db, tenant, world["a"], users=["bruno"])
        refused, text = tool(key, "korely_correct_fact", fact_id=world["fact"], object="Milan")
        assert refused and "error: not_found" in text, text
        assert facts_of_db(db, world["fact"]).invalid_at is None
        (row,) = trail(db, tenant, "facts:correct")
        assert (row.result, row.end_user_id, row.target_id) == ("error", None, world["fact"])

    def test_the_trail_has_ids_and_counts_never_the_text(self, db, tenant, world):
        tool(world["ka"], "korely_correct_fact", fact_id=world["fact"], object="Milan")
        (row,) = trail(db, tenant, "facts:correct")
        assert (row.result, row.actor, row.target_id, row.end_user_id) == (
            "ok", "mcp", world["fact"], "anna")
        assert row.project_id == world["a"].id and row.api_key_id is not None
        assert row.meta["facts_closed"] >= 1
        assert "Milan" not in str(row.meta) and "Rome" not in str(row.meta)
        tool(world["ka"], "korely_correct_fact", fact_id=world["fact"], close=True)
        (closed,) = trail(db, tenant, "facts:forget")
        assert closed.target_id == world["fact"] and closed.actor == "mcp"


def facts_of_db(db, public_id):
    from korely_agent.services.ids import parse_fact_id

    db.expire_all()
    return db.get(AgentFact, parse_fact_id(public_id))


class TestForgetAMemory:
    def _fact_of(self, db, tenant, world):
        """A fact only the memory states: the memory 'said' it."""
        from korely_agent.services.ids import parse_mem_id

        f = facts_of_db(db, world["fact"])
        mid = parse_mem_id(world["mem"])
        f.source_memory_id, f.source_memory_ids, f.observation_count = mid, [str(mid)], 1
        db.commit()

    def test_forget_drops_the_memory_and_closes_its_facts_with_counts(self, client, db, tenant,
                                                                      world):
        self._fact_of(db, tenant, world)
        refused, text = tool(world["ka"], "korely_forget_memory", memory_id=world["mem"])
        assert not refused, text
        assert text.startswith(f"Forgotten {world['mem']}") and "Facts closed (kept in history): 1" in text
        assert client.get(f"/v1/memories/{world['mem']}", headers=world["head"]
                          ).status_code == 404
        assert facts_of(client, world["head"]) == []
        # History keeps the closed fact.
        assert world["fact"] in [f["id"] for f in facts_of(
            client, world["head"], include_invalidated="true")]
        (row,) = trail(db, tenant, "memories:delete")
        assert (row.result, row.actor, row.target_id, row.end_user_id) == (
            "ok", "mcp", world["mem"], "anna")
        # A forget keeps the text: its verb is `forget`, never `erase` (1.0.23).
        assert row.action == "forget"
        assert row.meta["facts_invalidated"] == 1 and "Rome" not in str(row.meta)

    def test_forgetting_twice_is_not_found_as_over_rest(self, client, world):
        assert tool(world["ka"], "korely_forget_memory", memory_id=world["mem"])[0] is False
        assert client.delete(f"/v1/memories/{world['mem']}", headers=world["head"]
                             ).status_code == 404
        refused, text = tool(world["ka"], "korely_forget_memory", memory_id=world["mem"])
        assert refused and "error: not_found" in text, text

    @pytest.mark.parametrize("how", ["unknown", "confined"])
    def test_a_refused_erasure_is_filed_as_an_erasure(self, db, tenant, world, how):
        """The two refusals after the id is read: a memory that is not there,
        another end user's for a confined key (the review's probe, 14 Nov 2026)."""
        from korely_agent.services.ids import mem_public_id

        if how == "unknown":
            key, mid = world["ka"], mem_public_id(uuid.uuid4())
        else:
            key, mid = _key(db, tenant, world["a"], users=["bruno"]), world["mem"]
        refused, text = tool(key, "korely_forget_memory", memory_id=mid, erase=True)
        assert refused and "not_found" in text, text
        rows = [r for r in trail(db, tenant, "memories:delete") if r.result == "error"]
        assert [r.action for r in rows] == ["erase"]

    @pytest.mark.parametrize("erase,verb", [(False, "forget"), (True, "erase")],
                             ids=["forget", "erase"])
    def test_a_refusal_before_the_act_says_which_act(self, db, tenant, world, erase, verb):
        """An id that does not parse is refused before anything is read, and
        its trail row says which act was asked, as REST's (1.0.23)."""
        refused, text = tool(world["ka"], "korely_forget_memory", memory_id="mem_nope",
                             erase=erase)
        assert refused and "error: not_found" in text, text
        (row,) = trail(db, tenant, "memories:delete")
        assert (row.action, row.result) == (verb, "error")

    def test_erase_removes_the_row_and_the_facts_only_it_stated(self, db, tenant, world):
        from korely_agent.services.ids import parse_mem_id

        self._fact_of(db, tenant, world)
        refused, text = tool(world["ka"], "korely_forget_memory", memory_id=world["mem"],
                             erase=True)
        assert not refused, text
        assert text.startswith(f"Erased {world['mem']} for good") and "facts removed: 1" in text
        db.expire_all()
        assert db.get(AgentMemory, parse_mem_id(world["mem"])) is None
        assert facts_of_db(db, world["fact"]) is None
        (row,) = trail(db, tenant, "memories:delete")
        assert row.meta["erase"] is True and row.meta["facts_deleted"] == 1
        assert row.action == "erase"

    def test_a_read_only_key_is_refused_and_changes_nothing(self, db, tenant, world):
        key = _key(db, tenant, world["a"], scopes=[READ])
        refused, text = tool(key, "korely_forget_memory", memory_id=world["mem"])
        assert refused and "memories:write" in text, text
        db.expire_all()
        assert db.query(AgentMemory).filter(AgentMemory.deleted_at.isnot(None)).count() == 0

    def test_an_unknown_id_is_not_found_and_in_the_trail(self, db, tenant, world):
        mid = "mem_" + uuid.uuid4().hex
        refused, text = tool(world["ka"], "korely_forget_memory", memory_id=mid)
        assert refused and "error: not_found" in text, text
        (row,) = trail(db, tenant, "memories:delete")
        assert (row.result, row.target_id, row.meta["code"]) == ("error", mid, "not_found")

    def test_another_projects_memory_is_not_found_and_untouched(self, db, tenant, world):
        for erase in (False, True):
            refused, text = tool(world["kb"], "korely_forget_memory", memory_id=world["mem"],
                                 erase=erase)
            assert refused and "error: not_found" in text, text
        db.expire_all()
        assert db.query(AgentMemory).count() == 1
        assert db.query(AgentMemory).one().deleted_at is None
        for r in trail(db, tenant, "memories:delete"):
            assert r.end_user_id is None, "another project's end user leaked into this trail"

    def test_a_key_confined_to_other_end_users_does_not_find_it(self, db, tenant, world):
        key = _key(db, tenant, world["a"], users=["bruno"])
        refused, text = tool(key, "korely_forget_memory", memory_id=world["mem"])
        assert refused and "error: not_found" in text, text
        db.expire_all()
        assert db.query(AgentMemory).one().deleted_at is None
        (row,) = trail(db, tenant, "memories:delete")
        assert (row.result, row.end_user_id, row.target_id) == ("error", None, world["mem"])


class TestTheRefusalsOfTheWriteTools:
    def test_the_month_s_quota_refuses_a_correction_and_is_in_the_trail(self, db, tenant, world,
                                                                        monkeypatch):
        from fastapi import HTTPException
        from korely_agent.core.extensions import hooks

        def over(*a, **k):
            raise HTTPException(429, {"code": "quota_exceeded", "message": "month used"})
        monkeypatch.setattr(hooks, "before_write", over)
        refused, text = tool(world["ka"], "korely_correct_fact", fact_id=world["fact"],
                             object="Milan")
        assert refused and "quota_exceeded" in text, text
        assert facts_of_db(db, world["fact"]).invalid_at is None
        (row,) = trail(db, tenant, "facts:correct")
        assert (row.result, row.actor, row.target_id, row.meta["code"]) == (
            "error", "mcp", world["fact"], "quota_exceeded")

    def test_a_model_that_did_not_answer_is_service_unavailable_and_writes_nothing(
            self, db, tenant, world, monkeypatch):
        from korely_agent.services import facts as agent_facts

        def down(*a, **k):
            raise agent_facts.MODEL_FAILURES[0]("down")
        monkeypatch.setattr(agent_facts, "correct_fact", down)
        refused, text = tool(world["ka"], "korely_correct_fact", fact_id=world["fact"],
                             object="Milan")
        assert refused and "model_unavailable" in text, text
        assert facts_of_db(db, world["fact"]).invalid_at is None
        (row,) = trail(db, tenant, "facts:correct")
        assert (row.result, row.meta["code"], row.end_user_id) == (
            "error", "model_unavailable", "anna")

    def test_closing_before_the_fact_began_is_refused(self, client, db, tenant, world):
        fid = client.post("/v1/facts", headers=world["head"], json={
            "subject": "anna", "predicate": "works_at", "object": "Acme", "user_id": "anna",
            "valid_from": "2026-06-01"}).json()["id"]
        refused, text = tool(world["ka"], "korely_correct_fact", fact_id=fid, close=True,
                             at="2026-01-01")
        assert refused and "invalid_request" in text, text
        assert facts_of_db(db, fid).invalid_at is None
        (row,) = trail(db, tenant, "facts:forget")
        assert (row.result, row.end_user_id, row.target_id, row.meta["code"]) == (
            "error", "anna", fid, "invalid_request")

    def test_close_for_a_confined_key_does_not_find_another_end_users_fact(self, db, tenant,
                                                                          world):
        key = _key(db, tenant, world["a"], users=["bruno"])
        refused, text = tool(key, "korely_correct_fact", fact_id=world["fact"], close=True)
        assert refused and "error: not_found" in text, text
        assert facts_of_db(db, world["fact"]).invalid_at is None
        (row,) = trail(db, tenant, "facts:forget")
        assert (row.result, row.end_user_id, row.target_id) == ("error", None, world["fact"])

    def test_a_blank_at_is_absent_as_over_rest(self, world):
        refused, text = tool(world["ka"], "korely_correct_fact", fact_id=world["fact"],
                             object="Milan", at="")
        assert not refused, text


class TestBothDoorsKeepTheSameTrail:
    """The same action writes the same `meta` keys over REST and over MCP."""

    @staticmethod
    def _keys(db, tenant, endpoint, actor):
        rows = [r for r in trail(db, tenant, endpoint) if r.actor == actor]
        assert len(rows) == 1, rows
        return rows[0].result, set(rows[0].meta) - {"endpoint"}

    def _fact(self, client, world, obj):
        return client.post("/v1/facts", headers=world["head"], json={
            "subject": "anna", "predicate": "lives_in", "object": obj, "user_id": "anna"}
        ).json()["id"]

    def test_correct_and_close(self, client, db, tenant, world):
        client.patch(f"/v1/facts/{world['fact']}", headers=world["head"], json={"object": "Milan"})
        other = self._fact(client, world, "Oslo")
        tool(world["ka"], "korely_correct_fact", fact_id=other, object="Turin")
        assert self._keys(db, tenant, "facts:correct", "rest") == \
            self._keys(db, tenant, "facts:correct", "mcp")
        a, b = self._fact(client, world, "Bern"), self._fact(client, world, "Graz")
        client.post(f"/v1/facts/{a}/forget", headers=world["head"])
        tool(world["ka"], "korely_correct_fact", fact_id=b, close=True)
        assert self._keys(db, tenant, "facts:forget", "rest") == \
            self._keys(db, tenant, "facts:forget", "mcp")

    @pytest.mark.parametrize("erase", [False, True])
    def test_forget_and_erase(self, client, db, tenant, world, erase):
        second = client.post("/v1/memories", headers=world["head"], json={
            "content": "Anna likes tea.", "user_id": "anna"}).json()["id"]
        client.delete(f"/v1/memories/{world['mem']}", headers=world["head"],
                      params={"erase": str(erase).lower()})
        tool(world["ka"], "korely_forget_memory", memory_id=second, erase=erase)
        assert self._keys(db, tenant, "memories:delete", "rest") == \
            self._keys(db, tenant, "memories:delete", "mcp")
