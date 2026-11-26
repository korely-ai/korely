"""Every write says whose memory it is (1.0.19, the founder's decision of 12 Nov 2026).

A memory or a fact written with neither a `user_id` nor an `agent_id` was read
by the engine as one person's, "user", and the judge compared the facts of
every writer that sent none as that one person's. Now such a write is refused
at every door that writes one (POST /v1/memories, each row of POST /v1/batch,
POST /v1/facts) with a 422 `identity_required` that names the two fields; an
id made only of whitespace, format characters or marks names nobody. The
MCP's korely_add, which a person's assistant calls, is written where it was
before, the project's memory with no end user, under the name the client gave
when it connected; a client that gave none is held to the ids. What is not a
new write is unchanged: a job queued before the rule, an edit or a forget of
a row stored without ids, the walls of confined keys, the other callers
that read the ids by the same rules with no identity asked. No model is
called.
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from korely_agent.db.models import AgentBatchJob, AgentFact, AgentMemory, AuditEvent
from korely_agent.mcp import server as mcp_server
from korely_agent.services import batch
from korely_agent.services import keys as key_service
from korely_agent.services import memory_write
from korely_agent.services.ids import mem_public_id


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
    return key_service.create_api_key(db, tenant_id=tenant.id,
                                      name=f"k-{uuid.uuid4().hex[:6]}", **kw)


@pytest.fixture
def key(db, tenant):
    return _key(db, tenant)


@pytest.fixture
def head(key):
    return {"Authorization": f"Bearer {key[1]}"}


def mcp(full_key, tool, args):
    """What the agent reads: the result, or the words of an MCP error."""
    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(full_key) is not None
        try:
            return str(await mcp_server._get_agent_mcp().call_tool(tool, args))
        except ToolError as e:
            return str(e).split(": ", 1)[1] if str(e).startswith("Error executing tool") else str(e)
    return asyncio.run(go())


def _memories(db, tenant):
    db.expire_all()
    return db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant.id).all()


def _refused_for_identity(r, where=""):
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "identity_required"
    assert body["detail"]["code"] == "identity_required"
    assert body["message"].startswith(f"{where}user_id or {where}agent_id is required"), body


# ---------------------------------------------------------------- REST: one memory


# Format characters (Cf), marks (M*) and whitespace, the review's probes of
# 12 Nov 2026: each alone, and a few together.
INVISIBLE = {"U+200B": "\u200b", "U+FEFF": "\ufeff", "U+2060": "\u2060", "U+200E": "\u200e",
             "U+00AD": "\u00ad", "U+180E": "\u180e", "U+034F": "\u034f", "U+0301": "\u0301",
             "U+E0020": "\U000e0020", "U+3000": "\u3000", "U+2003 and a space": "\u2003 ",
             "spaces": "   ", "a joiner and a variation selector": "\u200d\ufe0f"}
NO_IDENTITY = ([{}, {"user_id": None, "agent_id": None}, {"run_id": "run-1"}]
               + [{"agent_id": v} for v in INVISIBLE.values()]
               + [{"user_id": v} for v in INVISIBLE.values()]
               + [{"user_id": "\u200b", "agent_id": "\ufeff"}])
NO_IDENTITY_IDS = (["nothing", "explicit nulls", "run_id alone"]
                   + [f"agent_id {k}" for k in INVISIBLE] + [f"user_id {k}" for k in INVISIBLE]
                   + ["both invisible"])


@pytest.mark.parametrize("ids", NO_IDENTITY, ids=NO_IDENTITY_IDS)
def test_a_memory_with_no_identity_is_refused_and_nothing_is_stored(client, head, db, tenant, ids):
    r = client.post("/v1/memories", headers=head, json={"content": "We ship on Fridays.", **ids})
    _refused_for_identity(r)
    assert _memories(db, tenant) == []
    # A malformed write names nobody yet: no row in the trail, as every other.
    db.expire_all()
    assert db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id,
                                       AuditEvent.action == "write").count() == 0


@pytest.mark.parametrize("ids,user,agent", [
    ({"user_id": "anna"}, "anna", None),
    ({"agent_id": "planner"}, None, "planner"),
    ({"user_id": "anna", "agent_id": "planner"}, "anna", "planner"),
    ({"agent_id": " planner "}, None, " planner "),
    # One id that names someone is enough: the other is stored as sent.
    ({"user_id": "  ", "agent_id": "planner"}, "  ", "planner"),
    ({"user_id": "e\u0301", "agent_id": "\u200b"}, "e\u0301", "\u200b"),
    # Every alphabet's letters and digits name someone.
    ({"user_id": "\u5f20\u4e09"}, "\u5f20\u4e09", None),
    ({"agent_id": "\u0905\u0917\u094d"}, None, "\u0905\u0917\u094d"),
    ({"user_id": "7"}, "7", None),
    # The declared limit (docs/guide/limits.md): a filler Unicode files as a
    # letter, and a blank it files as a symbol, count.
    ({"agent_id": "\u3164"}, None, "\u3164"),
    ({"agent_id": "\u2800"}, None, "\u2800"),
], ids=["user", "agent", "both", "agent padded", "user of spaces with an agent",
        "a letter and its accent", "han", "devanagari", "a digit",
        "limit: hangul filler", "limit: braille blank"])
def test_either_id_is_an_identity(client, head, db, tenant, ids, user, agent):
    r = client.post("/v1/memories", headers=head, json={"content": "We ship on Fridays.", **ids})
    assert r.status_code == 201, r.text
    [m] = _memories(db, tenant)
    assert (m.end_user_id, m.agent_id) == (user, agent)


def test_the_message_says_what_to_add_and_the_document_says_it_too(client, head):
    r = client.post("/v1/memories", headers=head, json={"content": "We ship on Fridays."})
    message = r.json()["message"]
    assert "user_id (the end user it belongs to)" in message
    assert "agent_id (the agent writing it)" in message
    doc = client.get("/openapi.json").json()
    schemas = doc["components"]["schemas"]
    for body in ("AddBody", "BatchMemory", "FactBody"):
        for field in ("user_id", "agent_id"):
            assert "identity_required" in schemas[body]["properties"][field]["description"], (
                body, field)
    assert "identity_required" in schemas["ApiError"]["properties"]["code"]["description"]


def test_a_key_confined_to_one_end_user_gives_the_write_its_identity(client, db, tenant):
    _row, full = _key(db, tenant, end_users=["anna"])
    r = client.post("/v1/memories", headers={"Authorization": f"Bearer {full}"},
                    json={"content": "We ship on Fridays."})
    assert r.status_code == 201, r.text
    assert [m.end_user_id for m in _memories(db, tenant)] == ["anna"]


def test_a_key_confined_to_several_still_asks_for_a_user_id_as_before(client, db, tenant):
    _row, full = _key(db, tenant, end_users=["anna", "repo:*"])
    for ids in ({}, {"agent_id": "planner"}):
        r = client.post("/v1/memories", headers={"Authorization": f"Bearer {full}"},
                        json={"content": "We ship on Fridays.", **ids})
        assert r.status_code == 403, r.text
        assert "give a user_id" in r.json()["message"]
    assert _memories(db, tenant) == []


def test_a_bad_id_is_refused_by_its_own_rule_first(client, head):
    r = client.post("/v1/memories", headers=head,
                    json={"content": "We ship on Fridays.", "agent_id": ""})
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_request"


# ---------------------------------------------------------------- REST: batch


def test_a_batch_row_with_no_identity_refuses_the_batch_and_queues_nothing(client, head, db, tenant):
    r = client.post("/v1/batch", headers=head, json={"memories": [
        {"content": "One.", "user_id": "anna"},
        {"content": "Two.", "run_id": "run-1"},
        {"content": "Three.", "agent_id": "planner"}]})
    _refused_for_identity(r, where="memories[1].")
    db.expire_all()
    assert db.query(AgentBatchJob).filter(AgentBatchJob.tenant_id == tenant.id).count() == 0


def test_a_batch_whose_rows_all_say_whose_is_imported(client, head, db, tenant):
    r = client.post("/v1/batch", headers=head, json={"memories": [
        {"content": "One.", "user_id": "anna"}, {"content": "Two.", "agent_id": "planner"}]})
    assert r.status_code == 202, r.text
    batch.process_batch_jobs(db)
    assert sorted((m.end_user_id or "", m.agent_id or "") for m in _memories(db, tenant)) == [
        ("", "planner"), ("anna", "")]


def test_a_job_queued_before_the_rule_is_written_as_it_was_promised(db, tenant):
    """A job accepted (202) by a release before 1.0.19 holds rows with no
    identity: they can be stored, and the worker writes them."""
    job = AgentBatchJob(tenant_id=tenant.id, received=1,
                        payload=[{"content": "Queued before the upgrade.", "user_id": None,
                                  "agent_id": None, "run_id": None, "metadata": None,
                                  "timestamp": None}])
    db.add(job)
    db.commit()
    batch.process_batch_jobs(db)
    db.expire_all()
    job = db.get(AgentBatchJob, job.id)
    assert (job.status, job.imported, job.failed) == ("completed", 1, 0), job.errors
    [m] = _memories(db, tenant)
    assert (m.end_user_id, m.agent_id) == (None, None)


def test_the_rule_is_the_choice_of_the_doors_that_write():
    """`identity` is off by default: the doors that write a memory or a fact
    turn it on, and nothing else reading ids by these rules is held to it
    (a caller that files something that is no memory, and the batch worker,
    which writes what its door placed)."""
    import inspect

    from korely_agent.api import batch as batch_door
    from korely_agent.api import facts as facts_door

    assert inspect.signature(memory_write.checked_ids).parameters["identity"].default is False
    assert inspect.signature(memory_write.checked).parameters["identity"].default is False
    assert inspect.signature(memory_write.write).parameters["identity"].default is True
    assert "identity=True" in inspect.getsource(batch_door.create_batch)
    assert "identity=True" in inspect.getsource(facts_door.create_fact)
    assert "identity" not in inspect.getsource(batch.process_batch_jobs)
    # A caller that reads ids for what they are: no identity asked.
    assert memory_write.checked_ids(user_id=None, agent_id=None, run_id=None) is None
    with pytest.raises(Exception) as e:
        memory_write.checked_ids(user_id=None, agent_id="\u200b", run_id=None, identity=True)
    assert e.value.detail["code"] == "identity_required"


@pytest.mark.parametrize("door", ["memory", "batch", "fact"])
def test_a_write_refused_for_its_identity_takes_nothing_of_the_quota(client, head, monkeypatch,
                                                                     door):
    """The identity is checked before `admit`: on the Cloud a refused write
    counts against neither the plan's writes nor a trial's, nor holds an
    agent name (hooks.before_write, before_new_agent_id)."""
    from korely_agent.core.extensions import hooks

    asked = []
    monkeypatch.setattr(hooks, "before_write", lambda *a, **k: asked.append("write"))
    monkeypatch.setattr(hooks, "before_new_agent_id", lambda *a, **k: asked.append("agent"))
    path, body = {"memory": ("/v1/memories", {"content": "x", "agent_id": "\u200b"}),
                  "batch": ("/v1/batch", {"memories": [{"content": "x", "agent_id": "\u200b"}]}),
                  "fact": ("/v1/facts", {"subject": "a", "predicate": "likes", "object": "c",
                                         "agent_id": "\u200b"})}[door]
    _refused_for_identity(client.post(path, headers=head, json=body),
                          where="memories[0]." if door == "batch" else "")
    assert asked == []
    named = {**body, "agent_id": "planner"} if door != "batch" else {
        "memories": [{"content": "x", "agent_id": "planner"}]}
    assert client.post(path, headers=head, json=named).status_code in (201, 202)
    assert "write" in asked


# ---------------------------------------------------------------- REST: a fact


def test_a_fact_with_no_identity_is_refused(client, head, db, tenant):
    fact = {"subject": "Anna", "predicate": "lives_in", "object": "Turin"}
    r = client.post("/v1/facts", headers=head, json=fact)
    _refused_for_identity(r)
    r = client.post("/v1/facts", headers=head, json={**fact, "agent_id": " "})
    _refused_for_identity(r)
    db.expire_all()
    assert db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id).count() == 0
    r = client.post("/v1/facts", headers=head, json={**fact, "user_id": "anna"})
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------- what is not a new write


def _written_before_the_upgrade(db, tenant, key):
    """A memory with no ids in the key's project, as a release before 1.0.19 stored it."""
    from korely_agent.core.auth import key_project
    from korely_agent.services.memory_engine import AgentMemoryEngine

    m = AgentMemoryEngine(db, tenant.id, key_project(db, key[0])).add("Written before the upgrade.")
    db.commit()
    return m


def test_a_memory_stored_without_ids_can_still_be_edited_and_forgotten(client, head, db, tenant,
                                                                     key):
    """Nothing is rewritten: a row from before 1.0.19 keeps its ids (none),
    and its edits and its deletion are open as before."""
    m = _written_before_the_upgrade(db, tenant, key)
    public = mem_public_id(m.id)
    r = client.patch(f"/v1/memories/{public}", headers=head,
                     json={"content": "Edited after the upgrade."})
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] is None and r.json()["agent_id"] is None
    r = client.delete(f"/v1/memories/{public}", headers=head)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- MCP


@pytest.fixture
def named_client(monkeypatch):
    """The name a client gives in `initialize`, as an MCP session holds it:
    a tool called in-process has no session (`_client_of` is None)."""
    monkeypatch.setattr(mcp_server, "_client_of", lambda server: "claude-code")


def test_the_mcp_writes_a_call_that_names_nobody_where_it_did_before(db, tenant, key,
                                                                     named_client):
    out = mcp(key[1], "korely_add", {"content": "We ship on Fridays."})
    assert "Stored mem_" in out, out
    [m] = _memories(db, tenant)
    assert (m.end_user_id, m.agent_id, m.client) == (None, None, "claude-code")


def test_an_mcp_client_that_gave_no_name_is_held_to_the_ids(db, tenant, key):
    out = mcp(key[1], "korely_add", {"content": "We ship on Fridays."})
    assert out.startswith("error: identity_required, user_id or agent_id is required"), out
    out = mcp(key[1], "korely_add", {"content": "We ship on Fridays.", "user_id": "   "})
    assert out.startswith("error: identity_required"), out
    assert _memories(db, tenant) == []
    assert "Stored mem_" in mcp(key[1], "korely_add", {"content": "We ship.", "user_id": "anna"})


@pytest.mark.parametrize("args,user,agent", [
    ({"user_id": "anna"}, "anna", None),
    ({"agent_id": "planner"}, None, "planner"),
    ({"user_id": "anna", "agent_id": "planner"}, "anna", "planner"),
    # As before 1.0.19: an id of spaces is stored as sent, never replaced.
    ({"user_id": "   "}, "   ", None),
], ids=["user", "agent", "both", "user of spaces"])
def test_the_mcp_keeps_the_ids_the_call_gives(db, tenant, key, named_client, args, user, agent):
    out = mcp(key[1], "korely_add", {"content": "We ship on Fridays.", **args})
    assert "Stored mem_" in out, out
    [m] = _memories(db, tenant)
    assert (m.end_user_id, m.agent_id) == (user, agent)


def test_the_mcp_adds_nothing_to_a_confined_keys_rule(db, tenant, named_client):
    _row, one = _key(db, tenant, end_users=["anna"])
    assert "Stored mem_" in mcp(one, "korely_add", {"content": "We ship on Fridays."})
    assert [m.end_user_id for m in _memories(db, tenant)] == ["anna"]
    _row, several = _key(db, tenant, end_users=["anna", "repo:*"])
    out = mcp(several, "korely_add", {"content": "Deploys need a review."})
    assert out.startswith("error: forbidden"), out
    assert "give a user_id" in out
    assert len(_memories(db, tenant)) == 1


@pytest.mark.parametrize("users", [["default"], ["d*"], ["default", "anna"]],
                         ids=["exact", "prefix", "two"])
def test_no_confined_key_reaches_what_an_assistant_wrote(client, db, tenant, key, named_client,
                                                         users):
    """The review's probe of 12 Nov 2026: what an assistant writes with no
    ids is in the memory with no end user, as before 1.0.19, which no key
    confined to end users reads or erases, whatever its list names."""
    mcp(key[1], "korely_add", {"content": "Assistant note: the deploy rotates monthly."})
    _c, confined = _key(db, tenant, end_users=users)
    h = {"Authorization": f"Bearer {confined}"}
    assert client.get("/v1/memories", headers=h).json()["memories"] == []
    r = client.delete("/v1/users/default/memories", headers=h)
    assert r.status_code in (200, 403), r.text
    assert len(_memories(db, tenant)) == 1


def test_an_assistants_facts_before_and_after_the_upgrade_are_one_memory(db, tenant, key,
                                                                          named_client):
    """A fact written before 1.0.19 with no ids, and the facts of what an
    assistant writes after it, are in the same memory: a change told after
    the upgrade closes the old value, as it did before."""
    from korely_agent.core.auth import key_project
    from korely_agent.db.models import AgentFact
    from korely_agent.services import facts as agent_facts

    project = key_project(db, key[0])
    agent_facts.write_fact_direct(
        db, tenant.id, subject="user", predicate="prefers", obj="dark theme", end_user_id=None,
        agent_id=None, run_id=None, subject_type="unknown", object_is_literal=False,
        confidence=0.9, valid_from=None, project_id=project, tense="current")
    db.commit()
    mcp(key[1], "korely_add", {"content": "I no longer like the dark theme."})
    [m] = _memories(db, tenant)
    # What its extraction would write, with the memory's own ids.
    agent_facts.write_fact_direct(
        db, tenant.id, subject="user", predicate="prefers", obj="dark theme",
        end_user_id=m.end_user_id, agent_id=m.agent_id, run_id=None, subject_type="unknown",
        object_is_literal=False, confidence=0.9, valid_from=None, project_id=project,
        tense="past")
    db.commit()
    db.expire_all()
    still_open = (db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id,
                                             AgentFact.invalid_at.is_(None)).all())
    assert still_open == [], [(f.object_label, f.end_user_id, f.tense) for f in still_open]


def test_the_tool_tells_the_model():
    tools = {t.name: t for t in asyncio.run(mcp_server._get_agent_mcp().list_tools())}
    assert "no end user" in tools["korely_add"].description
    assert "agent_id" in tools["korely_add"].description
    assert "agent_id" in mcp_server._AGENT_MCP_INSTRUCTIONS
