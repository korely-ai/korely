"""An `admin` key crosses the account's projects on /admin alone (review of
15 Nov 2026).

GET /admin/scopes said `admin` opens "the memories and facts of every
project of the account, whatever the key's own project", and a reader asked
whether that held on /v1 too. It does not: every /v1 route and every MCP
tool confines a key to its own project, whatever its scopes
(services/keys.key_project), and only the /admin routes read the whole
account. The words now say where, and this file holds the two sides: the
same admin key of project A reads nothing of project B on /v1 and the MCP,
and reads B on /admin. Zep and Mem0 make the data API one project per key
too (help.getzep.com/v2/projects; docs.mem0.ai, organizations and projects).
"""
import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import keys as key_service
from korely_agent.services import projects
from korely_agent.services.facts.service import write_fact_direct

ADMIN = list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE]
# The same end user and agent in both projects, so that a read across the
# wall shows as the other project's marker, not only as another name.
PERSON, AGENT = "shared-person", "support-bot"


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


def _fill(db, tenant_id, project, mark):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant_id, project_id=project.id,
                    end_user_id=PERSON, agent_id=AGENT, content=f"memory {mark}")
    db.add(m)
    db.flush()
    db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant_id, project_id=project.id,
                     end_user_id=PERSON, agent_id=AGENT, subject_label=PERSON,
                     predicate_raw="likes", predicate_canonical="likes",
                     object_label=mark, source_memory_id=m.id,
                     valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    db.commit()


@pytest.fixture
def two(db, tenant):
    a = projects.create_project(db, tenant_id=tenant.id, name="A")
    b = projects.create_project(db, tenant_id=tenant.id, name="B")
    _fill(db, tenant.id, a, "ZZMARKOFA")
    _fill(db, tenant.id, b, "ZZMARKOFB")
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name="admin-of-a",
                                          scopes=ADMIN, project_id=a.id)
    return a, b, full


# The /v1 reads of the data, with what each needs to list everything.
V1_READS = [
    ("/v1/memories", {}),
    ("/v1/facts", {"include_invalidated": True}),
    ("/v1/users", {}),
    ("/v1/agents", {}),
]


@pytest.mark.parametrize("path,params", V1_READS, ids=[p for p, _ in V1_READS])
def test_on_v1_an_admin_key_reads_its_own_project_only(client, two, path, params):
    _a, _b, full = two
    r = client.get(path, params=params, headers={"Authorization": f"Bearer {full}"})
    assert r.status_code == 200, r.text
    if path in ("/v1/memories", "/v1/facts"):
        assert "ZZMARKOFA" in r.text, r.text
    assert "ZZMARKOFB" not in r.text, r.text
    if path == "/v1/agents":
        # One agent, counted in A alone: B's memory is not in the count.
        (agent,) = r.json()["agents"]
        assert agent["memories"] == 1, agent


def test_through_the_mcp_too(db, tenant, two):
    """The MCP's tools take the key's project from the same rule
    (mcp/server: key_project at the verification)."""
    from korely_agent.mcp import server as mcp_server

    _a, _b, full = two
    a, b = two[0], two[1]
    write_fact_direct(db, tenant.id, subject=PERSON, predicate="keeps", obj="ZZMCPOFA",
                      end_user_id=PERSON, project_id=a.id)
    write_fact_direct(db, tenant.id, subject=PERSON, predicate="keeps", obj="ZZMCPOFB",
                      end_user_id=PERSON, project_id=b.id)
    db.commit()

    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(full) is not None
        facts = mcp_server._get_agent_mcp()._tool_manager._tools["korely_get_facts"].fn
        return await facts(user_id=PERSON)

    seen = asyncio.run(go())
    assert "ZZMCPOFA" in seen and "ZZMCPOFB" not in seen, seen


def test_on_admin_the_same_key_reads_every_project(client, two):
    _a, _b, full = two
    r = client.get("/admin/memories", headers={"Authorization": f"Bearer {full}"})
    assert r.status_code == 200, r.text
    assert "memory ZZMARKOFA" in r.text and "memory ZZMARKOFB" in r.text


def test_the_scopes_say_where_admin_crosses_projects(client):
    opens = {s["name"]: s["opens"] for s in client.get("/admin/scopes").json()["scopes"]}
    assert "through the /admin API alone, the memories and facts of every project" \
        in opens["admin"]
    assert "On /v1 and the MCP an admin key reads and writes its own project only" \
        in opens["admin"]
