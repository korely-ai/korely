"""Keys confined to some end users of their project (16 Oct 2026).

services/user_scope.py has the rules. What has to hold, for a key that may see
`founder` and `repo:*` in a project that also holds `private` and the shared
memory (no end user): every read without a user_id returns its own end users
only; a user_id outside the list is a 403; a row of another end user asked for
by its id is a 404, and is still there afterwards; a write outside the list is
refused, and a write without a user_id goes to the list's single id when it has
one; the listings name its own end users and their agents only; the context
block carries none of the others' facts; the trail shows its own end users'
events; the MCP tools refuse what REST refuses. And the rules themselves:
`*` at the end only, `%` and `_` characters of an id, never patterns; no
`admin` on a confined key.
"""
import asyncio
import uuid

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from korely_agent.db.models import AgentFact, AgentMemory, AuditEvent
from korely_agent.services import keys as key_service
from korely_agent.services import user_scope
from korely_agent.services.sentinels import EVERY_END_USER, EndUsers

SCOPE = ["founder", "repo:*"]
SECRET = "ZZPRIVATEMARK"


# ── the rules ───────────────────────────────────────────────────────────────

def test_the_list_is_checked():
    assert user_scope.checked(None) is None
    assert user_scope.checked([" founder ", "repo:*", "founder"]) == ["founder", "repo:*"]
    for bad in ([], ["*"], ["re*po"], ["a/b"], [""], "founder", [3]):
        with pytest.raises(ValueError):
            user_scope.checked(bad)


def test_who_it_allows():
    assert user_scope.allows(SCOPE, "founder") and user_scope.allows(SCOPE, "repo:korely")
    assert not user_scope.allows(SCOPE, "private") and not user_scope.allows(SCOPE, "founder2")
    assert not user_scope.allows(SCOPE, None)          # the shared memory is not theirs
    assert user_scope.allows(None, "private") and user_scope.allows(None, None)


def test_reads_and_writes_resolve_their_end_user():
    assert user_scope.for_read(None, None, EVERY_END_USER) is EVERY_END_USER
    assert isinstance(user_scope.for_read(SCOPE, None, EVERY_END_USER), EndUsers)
    assert user_scope.for_read(SCOPE, "repo:x", EVERY_END_USER) == "repo:x"
    with pytest.raises(user_scope.OutOfScope):
        user_scope.for_read(SCOPE, "private", EVERY_END_USER)
    assert user_scope.for_write(["founder"], None) == "founder"
    with pytest.raises(user_scope.OutOfScope):
        user_scope.for_write(SCOPE, None)              # two entries: say which
    with pytest.raises(user_scope.OutOfScope):
        user_scope.for_write(["repo:*"], None)         # a prefix is no default


def test_admin_is_never_confined(db, tenant):
    with pytest.raises(key_service.BadEndUsers):
        key_service.create_api_key(db, tenant_id=tenant.id, name="x",
                                   scopes=["memories:read", "admin"], end_users=["founder"])


# ── a project with four end users and two keys ─────────────────────────────

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


def _key(db, tenant, end_users=None, scopes=("memories:read", "memories:write")):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id,
                                            name=f"k_{uuid.uuid4().hex[:6]}",
                                            scopes=list(scopes), end_users=end_users)
    return full


@pytest.fixture
def world(client, db, tenant):
    from korely_agent.core.auth import key_project
    from korely_agent.services.memory_engine import AgentMemoryEngine

    whole = _key(db, tenant)
    row = key_service.verify_api_key(db, whole)
    project = key_project(db, row)
    engine = AgentMemoryEngine(db, tenant.id, project)
    mems, facts = {}, {}
    for user, text in (("founder", "Founder note: the pricing page ships Monday."),
                       ("repo:korely", "Repo note: the migrations run at boot."),
                       ("private", f"Private note {SECRET}: the health appointment."),
                       (None, f"Shared note {SECRET}: the office wifi.")):
        m = engine.add(text, end_user_id=user, agent_id=f"agent-{user or 'shared'}")
        mems[user] = m.id
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project,
                      end_user_id=user, agent_id=f"agent-{user or 'shared'}",
                      subject_label=(user or "office"), subject_type="unknown",
                      predicate_raw="note", predicate_canonical="note",
                      object_label=text, object_is_literal=True, confidence=0.9,
                      source_memory_id=m.id)
        db.add(f)
        facts[user] = f.id
        db.add(AuditEvent(tenant_id=tenant.id, project_id=project, actor="rest",
                          action="write", end_user_id=user))
    db.commit()
    confined = _key(db, tenant, end_users=SCOPE)
    return {"whole": {"Authorization": f"Bearer {whole}"},
            "confined": {"Authorization": f"Bearer {confined}"},
            "confined_raw": confined, "mems": mems, "facts": facts, "project": project}


def _mid(world, user):
    from korely_agent.services.ids import mem_public_id
    return mem_public_id(world["mems"][user])


def _fid(world, user):
    from korely_agent.services.ids import fact_public_id
    return fact_public_id(world["facts"][user])


class TestReads:
    def test_a_listing_returns_its_end_users_only(self, client, world):
        got = client.get("/v1/memories", headers=world["confined"]).json()
        users = {m["user_id"] for m in got["memories"]}
        assert users == {"founder", "repo:korely"}
        assert SECRET not in str(got)

    def test_the_whole_project_key_still_sees_everything(self, client, world):
        got = client.get("/v1/memories", headers=world["whole"]).json()
        assert {m["user_id"] for m in got["memories"]} == {"founder", "repo:korely", "private", None}

    @pytest.mark.parametrize("path", ["/v1/memories?user_id=private", "/v1/facts?user_id=private",
                                      "/v1/profile?user_id=private", "/v1/events?user_id=private",
                                      "/v1/audit?user_id=private",
                                      "/v1/context?query=note&user_id=private"])
    def test_an_end_user_outside_is_a_403(self, client, world, path):
        r = client.get(path, headers=world["confined"])
        assert r.status_code == 403, (path, r.text)

    def test_facts_without_a_user_are_its_own(self, client, world):
        got = client.get("/v1/facts", headers=world["confined"]).json()
        assert {f["user_id"] for f in got["facts"]} == {"founder", "repo:korely"}

    def test_a_row_of_another_end_user_is_not_found(self, client, world):
        for path in (f"/v1/memories/{_mid(world, 'private')}",
                     f"/v1/memories/{_mid(world, 'private')}/history",
                     f"/v1/memories/{_mid(world, None)}"):
            assert client.get(path, headers=world["confined"]).status_code == 404, path
        assert client.get(f"/v1/memories/{_mid(world, 'founder')}",
                          headers=world["confined"]).status_code == 200

    def test_the_context_carries_none_of_the_others(self, client, world):
        r = client.get("/v1/context?query=note", headers=world["confined"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert SECRET not in (body.get("context") or "") + (body.get("stable") or "") + \
            (body.get("volatile") or "")
        assert _fid(world, "private") not in body.get("sources", [])

    def test_the_listings_name_its_own_end_users_and_agents(self, client, world):
        users = client.get("/v1/users", headers=world["confined"]).json()
        assert {u["user_id"] for u in users["users"]} == {"founder", "repo:korely"}
        agents = client.get("/v1/agents", headers=world["confined"]).json()
        assert {a["agent_id"] for a in agents["agents"]} == {"agent-founder", "agent-repo:korely"}

    def test_the_trail_shows_its_own_end_users(self, client, world):
        got = client.get("/v1/audit?action=write", headers=world["confined"]).json()
        assert {e["user_id"] for e in got["events"]} == {"founder", "repo:korely"}


class TestWrites:
    def test_a_write_outside_is_refused(self, client, world, db):
        r = client.post("/v1/memories", headers=world["confined"],
                        json={"content": "sneaky", "user_id": "private"})
        assert r.status_code == 403
        assert db.query(AgentMemory).filter(AgentMemory.content == "sneaky").count() == 0

    def test_a_write_without_a_user_names_one_or_is_refused(self, client, world, db, tenant):
        assert client.post("/v1/memories", headers=world["confined"],
                           json={"content": "nobody"}).status_code == 403
        one = {"Authorization": f"Bearer {_key(db, tenant, end_users=['founder'])}"}
        r = client.post("/v1/memories", headers=one, json={"content": "to the founder"})
        assert r.status_code == 201 and r.json()["user_id"] == "founder"

    def test_a_write_inside_a_prefix_lands(self, client, world):
        r = client.post("/v1/memories", headers=world["confined"],
                        json={"content": "a repo note", "user_id": "repo:website"})
        assert r.status_code == 201 and r.json()["user_id"] == "repo:website"

    def test_another_end_users_row_cannot_be_changed_or_deleted(self, client, world, db):
        mid = _mid(world, "private")
        assert client.patch(f"/v1/memories/{mid}", headers=world["confined"],
                            json={"content": "overwritten"}).status_code == 404
        assert client.delete(f"/v1/memories/{mid}", headers=world["confined"]).status_code == 404
        fid = _fid(world, "private")
        assert client.post(f"/v1/facts/{fid}/forget", headers=world["confined"]).status_code == 404
        assert client.patch(f"/v1/facts/{fid}", headers=world["confined"],
                            json={"object": "changed"}).status_code == 404
        db.expire_all()
        row = db.get(AgentMemory, world["mems"]["private"])
        assert row.deleted_at is None and SECRET in row.content
        assert db.get(AgentFact, world["facts"]["private"]).invalid_at is None

    def test_another_end_user_cannot_be_erased(self, client, world, db):
        assert client.delete("/v1/users/private/memories",
                             headers=world["confined"]).status_code == 403
        assert client.delete("/v1/agents/agent-private",
                             headers=world["confined"]).status_code == 403
        db.expire_all()
        assert db.get(AgentMemory, world["mems"]["private"]) is not None

    def test_a_batch_row_outside_refuses_the_batch(self, client, world):
        r = client.post("/v1/batch", headers=world["confined"],
                        json={"memories": [{"content": "ok", "user_id": "founder"},
                                           {"content": "no", "user_id": "private"}]})
        assert r.status_code == 403 and "memories[1].user_id" in r.text


class TestTheKeyItself:
    def test_admin_creates_and_lists_a_confined_key(self, client, db, tenant):
        _row, admin = key_service.create_api_key(db, tenant_id=tenant.id, name="adm",
                                                 scopes=["memories:read", "admin"])
        head = {"Authorization": f"Bearer {admin}"}
        r = client.post("/admin/keys", headers=head,
                        json={"name": "hermes", "scopes": ["memories:read"], "users": SCOPE})
        assert r.status_code in (200, 201), r.text
        assert r.json()["users"] == SCOPE
        listed = client.get("/admin/keys", headers=head).json()
        rows = listed["keys"] if isinstance(listed, dict) else listed
        assert any(k.get("users") == SCOPE for k in rows)
        bad = client.post("/admin/keys", headers=head,
                          json={"name": "worse", "scopes": ["admin"], "users": ["founder"]})
        assert bad.status_code == 422


class TestMcp:
    def tool(self, name):
        from korely_agent.mcp import server as mcp_server

        return mcp_server._get_agent_mcp()._tool_manager._tools[name].fn

    def call(self, key, name, **kw):
        from korely_agent.mcp import server as mcp_server

        async def go():
            await mcp_server.KorelyAgentKeyVerifier().verify_token(key)
            return await self.tool(name)(**kw)
        return asyncio.run(go())

    def test_the_tools_refuse_what_rest_refuses(self, world):
        key = world["confined_raw"]
        with pytest.raises(ToolError):
            self.call(key, "korely_get_facts", user_id="private")
        with pytest.raises(ToolError):
            self.call(key, "korely_add", content="no user named")
        with pytest.raises(ToolError):
            self.call(key, "korely_get_context", query="note", user_id="private")

    def test_and_read_only_their_own(self, world):
        out = self.call(world["confined_raw"], "korely_get_facts")
        assert "Founder note" in out and SECRET not in out
        ctx = self.call(world["confined_raw"], "korely_get_context", query="note")
        assert SECRET not in ctx


def test_a_percent_or_underscore_is_a_character(db, tenant):
    """`a_*` is the prefix `a_`, never `a` and one character: LIKE's own
    wildcards are escaped."""
    from korely_agent.services.memory_engine import AgentMemoryEngine

    engine = AgentMemoryEngine(db, tenant.id)
    engine.add("under", end_user_id="a_1")
    engine.add("other", end_user_id="ab1")
    engine.add("pct", end_user_id="50%off")
    confined = AgentMemoryEngine(db, tenant.id, end_users=["a_*", "50%*"])
    rows, _total = confined.get_all()
    assert {r["end_user_id"] for r in rows} == {"a_1", "50%off"}


# ── the search, with a vector ───────────────────────────────────────────────
# Without a vector /v1/context stops before searching and the search answers
# 503, so the tests above covered the facts only: removing the end users'
# filter from AgentMemoryEngine.search left this file green (audit of 17 Oct
# 2026). Here every memory and every question has the same vector, so the
# cosine ranks nothing out and only the wall decides what comes back.

@pytest.fixture
def searchable(world, db, monkeypatch):
    from korely_agent.core.llm import EMBEDDING_DIM

    vector = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    for mid in world["mems"].values():
        db.get(AgentMemory, mid).embedding = vector
    db.commit()
    monkeypatch.setattr("korely_agent.services.memory_engine.embed", lambda *a, **k: vector)
    monkeypatch.setattr("korely_agent.services.context.embed", lambda *a, **k: vector)
    return world


class TestSearch:
    def test_the_whole_project_key_finds_all_four(self, client, searchable):
        """The control: without it the test below passes on a search that
        finds nothing at all."""
        r = client.post("/v1/memories/search", headers=searchable["whole"],
                        json={"query": "note", "limit": 10})
        assert r.status_code == 200, r.text
        assert {m["user_id"] for m in r.json()["results"]} == {"founder", "repo:korely",
                                                               "private", None}

    def test_a_search_returns_its_end_users_only(self, client, searchable):
        r = client.post("/v1/memories/search", headers=searchable["confined"],
                        json={"query": "note", "limit": 10})
        assert r.status_code == 200, r.text
        assert {m["user_id"] for m in r.json()["results"]} == {"founder", "repo:korely"}
        assert SECRET not in r.text
        one = client.post("/v1/memories/search", headers=searchable["confined"],
                          json={"query": "note", "user_id": "private"})
        assert one.status_code == 403

    def test_the_context_searches_its_end_users_only(self, client, searchable):
        whole = client.get("/v1/context?query=note", headers=searchable["whole"]).json()
        assert SECRET in whole["context"], "the control: the memories reach the block"
        body = client.get("/v1/context?query=note", headers=searchable["confined"]).json()
        assert not body.get("degraded"), body
        assert "Founder note" in body["context"]
        assert SECRET not in body["context"]
        for user in ("private", None):
            assert _mid(searchable, user) not in body["sources"]

    def test_korely_search_finds_its_end_users_only(self, searchable):
        mcp = TestMcp()
        out = mcp.call(searchable["whole"]["Authorization"].split()[1], "korely_search",
                       query="note")
        assert SECRET in out, "the control"
        out = mcp.call(searchable["confined_raw"], "korely_search", query="note")
        assert "Founder note" in out and "Repo note" in out
        assert SECRET not in out
        with pytest.raises(ToolError):
            mcp.call(searchable["confined_raw"], "korely_search", query="note",
                     user_id="private")
