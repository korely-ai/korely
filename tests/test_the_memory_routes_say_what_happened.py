"""The memory routes say what happened, in the reply and in the trail
(audit of 17 Oct 2026).

- a read or a delete that answered 404 wrote `result: ok` in the trail, and
  the deletion of an agent namespace wrote no `target_id`;
- an empty id was a filter on '' on some routes (an empty store), a 422 on
  /v1/audit, a refusal for user_id and agent_id on a write, and a stored ''
  for run_id;
- PATCH answered an unreadable `expected_updated_at` with 409 stale_write,
  which a client retrying on it repeats for ever, and an empty one switched
  the check off;
- GET /v1/agents of a key confined to some end users said `used: 0` next to
  the agents it listed;
- PATCH without `expected_updated_at` wrote onto a memory forgotten while its
  vector was computed, and answered 200.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from korely_agent.db.models import AgentMemory, AuditEvent
from korely_agent.services import keys as key_service
from korely_agent.services.ids import mem_public_id
from korely_agent.services.memory_engine import AgentMemoryEngine



@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _key(db, tenant, end_users=None):
    _row, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name=f"t-{uuid.uuid4().hex[:8]}",
        scopes=["memories:read", "memories:write"],
        end_users=end_users)
    return {"Authorization": f"Bearer {full}"}


@pytest.fixture
def auth(db, tenant):
    return _key(db, tenant)


def _trail(db, tenant, endpoint):
    db.expire_all()
    return [r for r in db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id).all()
            if (r.meta or {}).get("endpoint") == endpoint]


class TestA404IsNotOk:
    def test_a_read_of_no_memory_is_not_ok(self, db, tenant, client, auth):
        nobody = mem_public_id(uuid.uuid4())
        assert client.get(f"/v1/memories/{nobody}", headers=auth).status_code == 404
        (row,) = _trail(db, tenant, "memories:get")
        assert (row.result, row.target_id) == ("error", nobody)

    def test_a_deletion_of_no_memory_is_not_ok(self, db, tenant, client, auth):
        nobody = mem_public_id(uuid.uuid4())
        assert client.delete(f"/v1/memories/{nobody}", headers=auth).status_code == 404
        (row,) = _trail(db, tenant, "memories:delete")
        assert (row.result, row.target_id) == ("error", nobody)

    def test_a_read_and_a_deletion_that_happened_are_ok(self, db, tenant, client, auth):
        m = client.post("/v1/memories", headers=auth,
                        json={"content": "Ada likes tea.", "user_id": "ada"}).json()
        assert client.get(f"/v1/memories/{m['id']}", headers=auth).status_code == 200
        assert client.delete(f"/v1/memories/{m['id']}", headers=auth).status_code == 200
        assert [r.result for r in _trail(db, tenant, "memories:get")] == ["ok"]
        assert [r.result for r in _trail(db, tenant, "memories:delete")] == ["ok"]

    def test_the_deletion_of_an_agent_names_it(self, db, tenant, client, auth):
        client.post("/v1/memories", headers=auth,
                    json={"content": "Ada likes tea.", "user_id": "ada", "agent_id": "support"})
        assert client.delete("/v1/agents/support", headers=auth).status_code == 200
        (row,) = _trail(db, tenant, "agents:delete")
        assert (row.result, row.target_id) == ("ok", "support")


class TestAnEmptyIdIsNeverAValue:
    @pytest.mark.parametrize("field", ["user_id", "agent_id", "run_id"])
    def test_the_list_refuses_it(self, client, auth, field):
        client.post("/v1/memories", headers=auth, json={"content": "Ada likes tea.", "user_id": "ada"})
        r = client.get("/v1/memories", headers=auth, params={field: ""})
        assert r.status_code == 422, r.text
        assert "empty" in r.text

    @pytest.mark.parametrize("field", ["user_id", "agent_id", "run_id"])
    def test_the_search_refuses_it(self, client, auth, field):
        r = client.post("/v1/memories/search", headers=auth, json={"query": "tea", field: ""})
        assert r.status_code == 422, r.text

    def test_the_events_and_the_facts_refuse_it(self, client, auth):
        assert client.get("/v1/events", headers=auth, params={"user_id": ""}).status_code == 422
        assert client.get("/v1/facts", headers=auth, params={"user_id": ""}).status_code == 422
        assert client.get("/v1/users", headers=auth, params={"agent_id": ""}).status_code == 422

    def test_a_write_refuses_an_empty_run(self, db, tenant, client, auth):
        r = client.post("/v1/memories", headers=auth,
                        json={"content": "Ada likes tea.", "user_id": "ada", "run_id": ""})
        assert r.status_code == 422, r.text
        db.expire_all()
        assert db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant.id).count() == 0


def test_the_context_refuses_an_empty_id_too(client, auth):
    for field in ("user_id", "agent_id"):
        r = client.get("/v1/context", headers=auth, params={"query": "tea", field: ""})
        assert r.status_code == 422, (field, r.text)


def _written(client, auth):
    m = client.post("/v1/memories", headers=auth,
                    json={"content": "Maria lives in Rome.", "user_id": "maria"}).json()
    return m["id"]


class TestTheVersionMustBeReadable:
    @pytest.mark.parametrize("version", ["yesterday", "", "  "])
    def test_an_unreadable_version_is_a_422_and_nothing_changes(self, db, client, auth, version):
        mid = _written(client, auth)
        r = client.patch(f"/v1/memories/{mid}", headers=auth,
                         json={"content": "Maria lives in Milan.", "expected_updated_at": version})
        assert r.status_code == 422, r.text
        assert "expected_updated_at" in r.text
        assert client.get(f"/v1/memories/{mid}", headers=auth).json()["content"] == "Maria lives in Rome."

    def test_the_field_says_so(self, client):
        spec = client.get("/openapi.json").json()
        field = spec["components"]["schemas"]["UpdateBody"]["properties"]["expected_updated_at"]
        assert "422" in field["description"]


class TestAConfinedKeyCountsWhatItLists:
    def test_used_counts_the_agents_it_sees(self, db, tenant, client):
        confined = _key(db, tenant, end_users=["ada"])
        r = client.post("/v1/memories", headers=confined,
                        json={"content": "Ada likes tea.", "user_id": "ada", "agent_id": "support"})
        assert r.status_code == 201, r.text
        out = client.get("/v1/agents", headers=confined).json()
        assert (out["total"], out["used"], out["cap"]) == (1, 1, 0)
        whole = client.get("/v1/agents", headers=_key(db, tenant)).json()
        assert (whole["total"], whole["used"]) == (1, 1)


class TestAnEditDoesNotLandOnAForgottenMemory:
    def _forgotten_while_embedding(self, engine, monkeypatch):
        def the_delete_lands(self_, m, text_, **kw):
            with engine.begin() as c:
                c.execute(text("UPDATE agent_memories SET deleted_at = now() WHERE id = :id"),
                          {"id": m.id})

        monkeypatch.setattr(AgentMemoryEngine, "_vector_of", the_delete_lands)

    def test_the_engine_writes_nothing(self, db, tenant, engine, monkeypatch):
        eng = AgentMemoryEngine(db, tenant.id)
        row = eng.add("Maria lives in Rome.", end_user_id="maria")
        self._forgotten_while_embedding(engine, monkeypatch)
        assert eng.update(str(row.id), "Maria lives in Milan.") is None
        db.expire_all()
        kept = db.get(AgentMemory, row.id)
        assert kept.deleted_at is not None
        assert kept.content == "Maria lives in Rome."
        assert kept.updated_at is None

    def test_the_route_answers_404(self, db, client, auth, engine, monkeypatch):
        mid = _written(client, auth)
        self._forgotten_while_embedding(engine, monkeypatch)
        r = client.patch(f"/v1/memories/{mid}", headers=auth, json={"content": "Maria lives in Milan."})
        assert r.status_code == 404, r.text
