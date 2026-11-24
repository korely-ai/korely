"""The read marks of the agent notices (services/read_marks.py, 1.0.15).

A read that names its reader keeps which facts it handed over as true, one
row per account, project, reader and fact; a read without one keeps nothing
and answers byte for byte as it does with one (and as before the marks).
The rows hold no text, stay behind the walls of the read that wrote them
(account, project, end user, agent), go with their person's erasure, their
project and their fact, and are purged READ_MARKS_RETENTION_DAYS after the
last read. A mark that cannot be written never fails the read.
"""
import asyncio
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import text

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentReadMark, Tenant
from korely_agent.mcp import server as mcp_server
from korely_agent.services import bookkeeping, read_marks
from korely_agent.services import context as ctx
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service
from korely_agent.services import vectors
from korely_agent.services.ids import fact_public_id

VECTOR = [0.1] * EMBEDDING_DIM


@pytest.fixture(autouse=True)
def _vector(monkeypatch):
    from korely_agent.services import memory_engine

    for module in (ctx, memory_engine):
        monkeypatch.setattr(module, "embed", lambda *a, **k: VECTOR)


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
def default(db, tenant):
    return project_service.get_or_create_default_project(db, tenant.id)


def _key(db, tenant, **kw):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id,
                                            name=f"k-{uuid.uuid4().hex[:6]}", **kw)
    return full


def _head(full):
    return {"Authorization": f"Bearer {full}"}


def fact(db, tenant_id, project_id, *, user=None, agent=None, subject="Maria", obj="Rome"):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant_id, project_id=project_id, end_user_id=user,
                  agent_id=agent, subject_label=subject, predicate_raw="lives in",
                  predicate_canonical="lives_in", object_label=obj,
                  valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                  embedding=VECTOR, embedding_status=vectors.DONE)
    db.add(f)
    db.commit()
    return f


def marks(db, **where):
    db.expire_all()
    q = db.query(AgentReadMark)
    for k, v in where.items():
        q = q.filter(getattr(AgentReadMark, k) == v)
    return q.all()


def mcp(full, tool, args):
    """The tool's answer, or the words of its refusal."""
    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(full) is not None
        try:
            return str(await mcp_server._get_agent_mcp().call_tool(tool, args))
        except ToolError as e:
            s = str(e)
            return s.split(": ", 1)[1] if s.startswith("Error executing tool") else s
    return asyncio.run(go())


# Every door that takes `reader`, as (REST path and params, MCP tool and args).
DOORS = {
    "context": (("/v1/context", {"query": "Where does Maria live?", "user_id": "u1"}),
                ("korely_get_context", {"query": "Where does Maria live?", "user_id": "u1"})),
    "facts": (("/v1/facts", {"user_id": "u1"}), ("korely_get_facts", {"user_id": "u1"})),
}


@pytest.fixture
def world(db, tenant, default):
    """Two facts of u1, one of u2, one of an agent's memory with no end user,
    in the Default project; one fact of u1 in project P2."""
    p2 = project_service.create_project(db, tenant_id=tenant.id, name="P2")
    return {
        "u1": [fact(db, tenant.id, default.id, user="u1"),
               fact(db, tenant.id, default.id, user="u1", subject="Maria", obj="Acme")],
        "u2": [fact(db, tenant.id, default.id, user="u2", subject="Luca", obj="Turin")],
        "agent": [fact(db, tenant.id, default.id, agent="planner", subject="the staging database",
                       obj="staging-db-2")],
        "p2": [fact(db, tenant.id, p2.id, user="u1", subject="Maria", obj="Milan")],
        "p2_project": p2,
    }


# ── only a read that names its reader marks, and the answer is the same ────

@pytest.mark.parametrize("door", sorted(DOORS))
def test_a_read_without_a_reader_keeps_nothing(client, db, tenant, world, door):
    full = _key(db, tenant)
    (path, params), (tool, args) = DOORS[door]
    assert client.get(path, params=params, headers=_head(full)).status_code == 200
    assert not mcp(full, tool, args).startswith("error")
    assert marks(db) == []
    assert "read_marks" not in bookkeeping.state(), "/health says nothing of the marks"


@pytest.mark.parametrize("door", sorted(DOORS))
def test_the_answer_is_the_same_byte_for_byte_with_a_reader(client, db, tenant, world, door):
    full = _key(db, tenant)
    (path, params), (tool, args) = DOORS[door]
    without = client.get(path, params=params, headers=_head(full))
    with_reader = client.get(path, params={**params, "reader": "support-bot"}, headers=_head(full))
    assert with_reader.status_code == without.status_code == 200
    if door == "context":
        # With nothing changed the block is the same; the answer adds the two
        # fields of agent notices, empty (tests/test_changes_in_the_block.py).
        told = with_reader.json()
        assert (told.pop("changed"), told.pop("changed_more")) == ([], 0)
        assert told == without.json()
        assert "changed" not in without.json()
    else:
        assert with_reader.content == without.content
    assert mcp(full, tool, {**args, "reader": "support-bot"}) == mcp(full, tool, args)
    assert marks(db), "the reads with a reader kept their marks"


@pytest.mark.parametrize("door", sorted(DOORS))
def test_a_reader_keeps_the_facts_it_was_handed(client, db, tenant, default, world, door):
    full = _key(db, tenant)
    (path, params), _mcp = DOORS[door]
    r = client.get(path, params={**params, "reader": "support-bot"}, headers=_head(full))
    if door == "context":
        handed = {s for s in r.json()["sources"] if s.startswith("fct_")}
    else:
        handed = {f["id"] for f in r.json()["facts"]}
    assert handed == {fact_public_id(f.id) for f in world["u1"]}
    rows = marks(db)
    assert {fact_public_id(m.fact_id) for m in rows} == handed
    assert {(m.tenant_id, m.project_id, m.reader, m.end_user_id, m.notified_at) for m in rows} \
        == {(tenant.id, default.id, "support-bot", "u1", None)}
    first = {m.fact_id: (m.first_read_at, m.last_read_at) for m in rows}

    client.get(path, params={**params, "reader": "support-bot"}, headers=_head(full))
    again = marks(db)
    assert len(again) == len(rows), "a second read moves the row, it makes no second one"
    for m in again:
        assert m.first_read_at == first[m.fact_id][0]
        assert m.last_read_at > first[m.fact_id][1]
    assert bookkeeping.state()["read_marks"]["state"] == "working"


@pytest.mark.parametrize("door", sorted(DOORS))
def test_the_mcp_twin_keeps_the_same_marks(db, tenant, default, world, door):
    full = _key(db, tenant)
    _rest, (tool, args) = DOORS[door]
    assert not mcp(full, tool, {**args, "reader": "support-bot"}).startswith("error")
    assert {m.fact_id for m in marks(db)} == {f.id for f in world["u1"]}
    assert {(m.project_id, m.reader) for m in marks(db)} == {(default.id, "support-bot")}


def _now():
    return datetime.now(timezone.utc).isoformat()


@pytest.mark.parametrize("params", [
    {"as_of": "2026-06-01"}, {"known_as_of": _now}, {"include_invalidated": "true"},
], ids=["as_of", "known_as_of", "include_invalidated"])
def test_a_read_of_the_past_or_of_the_history_keeps_nothing(client, db, tenant, world, params):
    full = _key(db, tenant)
    params = {k: (v() if callable(v) else v) for k, v in params.items()}
    r = client.get("/v1/facts", params={"user_id": "u1", "reader": "support-bot", **params},
                   headers=_head(full))
    assert r.status_code == 200 and r.json()["facts"], r.text
    assert not mcp(full, "korely_get_facts",
                   {"user_id": "u1", "reader": "support-bot",
                    **{k: (v == "true" if k == "include_invalidated" else v)
                       for k, v in params.items()}}).startswith("error")
    assert marks(db) == []


def test_a_context_of_the_past_keeps_nothing(client, db, tenant, world):
    full = _key(db, tenant)
    params = {"query": "Where does Maria live?", "user_id": "u1", "reader": "support-bot",
              "known_as_of": _now()}
    r = client.get("/v1/context", params=params, headers=_head(full))
    assert r.status_code == 200 and any(s.startswith("fct_") for s in r.json()["sources"])
    mcp(full, "korely_get_context", params)
    assert marks(db) == []


def test_the_dashboard_keeps_nothing(client, db, tenant, world):
    """The screen's reads take no reader: a parameter named so is not one of
    theirs, and nothing is marked."""
    admin = _key(db, tenant, scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    r = client.get("/admin/facts", params={"user_id": "u1", "reader": "support-bot"},
                   headers=_head(admin))
    assert r.status_code == 200, r.text
    assert marks(db) == []


# ── the reader's name, by the rule of an agent_id ───────────────────────────

@pytest.mark.parametrize("bad", ["", "ne\x00o", "n" * 256], ids=["empty", "nul", "too-long"])
@pytest.mark.parametrize("door", sorted(DOORS))
def test_a_reader_is_refused_as_an_agent_id_is(client, db, tenant, world, door, bad):
    full = _key(db, tenant)
    (path, params), (tool, args) = DOORS[door]
    r = client.get(path, params={**params, "reader": bad}, headers=_head(full))
    a = client.get(path, params={**params, "agent_id": bad}, headers=_head(full))
    assert r.status_code == a.status_code == 422, r.text
    assert r.json()["code"] == a.json()["code"] == "invalid_request"
    said = mcp(full, tool, {**args, "reader": bad})
    assert said.startswith("error: invalid_request, "), said
    assert marks(db) == []


@pytest.mark.parametrize("bad", ["a\nb", "\t", "a\x01b", "a\x85b", "a\u2028b", "a\u2029b", ".",
                                 "..", "a/b"],
                         ids=["newline", "tab", "control", "next-line", "line-separator",
                              "paragraph-separator", "dot", "dots", "slash"])
@pytest.mark.parametrize("door", sorted(DOORS))
def test_a_reader_follows_the_rule_of_a_written_agent_id(client, db, tenant, world, door, bad):
    """A reader is an agent's name, and its marks go with that namespace's
    deletion: what a write refuses as an agent_id (ids.namespace_id_problem)
    is refused as a reader on every door (review of piece 3: a read took a
    line break, '..' and a '/' by the looser rule of what a read narrows on,
    and DELETE /v1/agents/{agent_id} could never reach their marks)."""
    from korely_agent.services.ids import namespace_id_problem

    assert namespace_id_problem(bad), "the write refuses it as an agent_id"
    full = _key(db, tenant)
    (path, params), (tool, args) = DOORS[door]
    r = client.get(path, params={**params, "reader": bad}, headers=_head(full))
    assert r.status_code == 422 and r.json()["code"] == "invalid_request", r.text
    said = mcp(full, tool, {**args, "reader": bad})
    assert said.startswith("error: invalid_request, reader "), said
    assert marks(db) == []


def test_every_reader_is_reached_by_its_agents_deletion(client, db, tenant, default, world):
    """A name the agent_id rule takes, however odd (spaces, a right-to-left
    mark), is reached by DELETE /v1/agents/{agent_id}, its marks with it."""
    from urllib.parse import quote

    full = _key(db, tenant)
    # Other scripts, emoji and marks too: the rule refuses what cannot be
    # read back, by category, never a letter, a symbol or a mark.
    for name in ("  ", "bot\u202e", "\xa0", "\u30b5\u30dd\u30fc\u30c8", "\U0001F916",
                 "\u2764\ufe0f", "e\u0301", "\u092c\u0949\u091f"):
        assert client.get("/v1/facts", params={"reader": name},
                          headers=_head(full)).status_code == 200
        assert marks(db, reader=name)
        n = len(marks(db, reader=name))
        r = client.delete(f"/v1/agents/{quote(name, safe='')}", headers=_head(full))
        assert r.status_code == 200, r.text
        assert r.json()["read_marks_deleted"] == n > 0, "the answer counts the marks it took"
        assert marks(db, reader=name) == [], repr(name)


# ── the walls ───────────────────────────────────────────────────────────────

def test_the_account_wall(client, db, tenant, default, world):
    """The same reader in another account keeps its own marks only, and a
    mark asked with another account's fact ids keeps none."""
    other = Tenant(id=uuid.uuid4(), name="Other")
    db.add(other)
    db.commit()
    theirs = fact(db, other.id, project_service.get_or_create_default_project(db, other.id).id,
                  user="u1", obj="Paris")
    client.get("/v1/facts", params={"user_id": "u1", "reader": "support-bot"},
               headers=_head(_key(db, other)))
    assert {(m.tenant_id, m.fact_id) for m in marks(db)} == {(other.id, theirs.id)}
    read_marks.mark(other.id, theirs.project_id, "support-bot", [f.id for f in world["u1"]])
    read_marks.mark(tenant.id, default.id, "support-bot", [theirs.id])
    assert {(m.tenant_id, m.fact_id) for m in marks(db)} == {(other.id, theirs.id)}
    # Where no project tells them apart (an install with no Default project,
    # facts of no project in both accounts), the account still does.
    mine, also_theirs = fact(db, tenant.id, None, user="u1"), fact(db, other.id, None, user="u1")
    read_marks.mark(other.id, None, "support-bot", [mine.id])
    read_marks.mark(tenant.id, None, "support-bot", [also_theirs.id])
    assert {(m.tenant_id, m.fact_id) for m in marks(db)} == {(other.id, theirs.id)}


def test_the_project_wall(client, db, tenant, default, world):
    """A key of P2 marks P2's facts only, under P2; a mark asked in P2 with
    the Default project's fact ids keeps none."""
    p2 = world["p2_project"]
    client.get("/v1/facts", params={"user_id": "u1", "reader": "support-bot"},
               headers=_head(_key(db, tenant, project_id=p2.id)))
    assert {(m.project_id, m.fact_id) for m in marks(db)} == {(p2.id, world["p2"][0].id)}
    read_marks.mark(tenant.id, p2.id, "support-bot", [f.id for f in world["u1"]])
    assert {(m.project_id, m.fact_id) for m in marks(db)} == {(p2.id, world["p2"][0].id)}


def test_the_end_user_wall(client, db, tenant, world):
    """A key confined to u1 marks u1's facts only; a read of every end user
    marks each fact under its own end user, never the request's."""
    confined = _key(db, tenant, end_users=["u1"])
    client.get("/v1/facts", params={"reader": "support-bot"}, headers=_head(confined))
    assert {m.fact_id for m in marks(db)} == {f.id for f in world["u1"]}
    assert {m.end_user_id for m in marks(db)} == {"u1"}
    client.get("/v1/facts", params={"reader": "billing-bot"}, headers=_head(_key(db, tenant)))
    by_fact = {m.fact_id: m.end_user_id for m in marks(db, reader="billing-bot")}
    want = {f.id: f.end_user_id for k in ("u1", "u2", "agent") for f in world[k]}
    assert by_fact == want


def test_the_agent_and_each_reader(client, db, tenant, world):
    """A read narrowed to an agent marks that agent's facts only, and two
    readers of one fact are two rows."""
    full = _key(db, tenant)
    client.get("/v1/facts", params={"agent_id": "planner", "reader": "support-bot"},
               headers=_head(full))
    assert {m.fact_id for m in marks(db)} == {world["agent"][0].id}
    client.get("/v1/facts", params={"agent_id": "planner", "reader": "billing-bot"},
               headers=_head(full))
    readers = sorted(m.reader for m in marks(db, fact_id=world["agent"][0].id))
    assert readers == ["billing-bot", "support-bot"]
    assert marks(db, fact_id=world["agent"][0].id)[0].end_user_id is None


def test_a_project_of_none_is_one_project(db, tenant):
    """On an install with no Default project facts and marks have no
    project: a second read moves the same row (NULLS NOT DISTINCT)."""
    f = fact(db, tenant.id, None, user="u1")
    read_marks.mark(tenant.id, None, "support-bot", [f.id])
    read_marks.mark(tenant.id, None, "support-bot", [fact_public_id(f.id)])
    assert [(m.project_id, m.fact_id) for m in marks(db)] == [(None, f.id)]


# ── what takes the rows away ───────────────────────────────────────────────

def _mark_everything(client, db, tenant):
    full = _key(db, tenant)
    client.get("/v1/facts", params={"reader": "support-bot"}, headers=_head(full))
    return full


def test_an_end_users_erasure_takes_their_marks(client, db, tenant, world):
    full = _mark_everything(client, db, tenant)
    assert marks(db, end_user_id="u1")
    r = client.delete("/v1/users/u1/memories", headers=_head(full))
    assert r.status_code == 200, r.text
    assert marks(db, end_user_id="u1") == []
    assert {m.fact_id for m in marks(db)} == {world["u2"][0].id, world["agent"][0].id}


def test_an_end_users_erasure_takes_the_marks_read_under_their_id(client, db, tenant, world):
    """A caller that named its reader after an end user left that id in the
    marks of other people's facts: the end user's erasure takes those too,
    in the project it is made in, and no other reader's marks."""
    client.get("/v1/facts", params={"reader": "u1"}, headers=_head(_key(db, tenant)))
    client.get("/v1/facts", params={"reader": "u1"},
               headers=_head(_key(db, tenant, project_id=world["p2_project"].id)))
    full = _mark_everything(client, db, tenant)
    assert {m.end_user_id for m in marks(db, reader="u1")} == {"u1", "u2", None}
    assert client.delete("/v1/users/u1/memories", headers=_head(full)).status_code == 200
    left = marks(db, reader="u1")
    assert [m.project_id for m in left] == [world["p2_project"].id], \
        "only the reads of another project, behind the erasure's wall, stay"
    assert {m.fact_id for m in marks(db, reader="support-bot")} == \
        {world["u2"][0].id, world["agent"][0].id}


def test_an_agents_erasure_takes_the_marks_of_its_facts(client, db, tenant, default, world):
    from korely_agent.services.facts import service as facts

    _mark_everything(client, db, tenant)
    facts.delete_agent(db, tenant.id, "planner", project_id=default.id)
    assert marks(db, fact_id=world["agent"][0].id) == []
    assert len(marks(db)) == 3


def test_an_agents_erasure_takes_the_marks_read_under_its_name(client, db, tenant, default,
                                                               world):
    """A reader is an agent's name: the agent namespace's erasure takes the
    reads it made, of anybody's facts, in the project it is made in, and no
    other reader's (decided on 8 Nov 2026; review of piece 1: the
    mutant that took them lived)."""
    from korely_agent.services.facts import service as facts

    client.get("/v1/facts", params={"reader": "planner"}, headers=_head(_key(db, tenant)))
    client.get("/v1/facts", params={"reader": "planner"},
               headers=_head(_key(db, tenant, project_id=world["p2_project"].id)))
    _mark_everything(client, db, tenant)
    assert len(marks(db, reader="planner")) == 5
    facts.delete_agent(db, tenant.id, "planner", project_id=default.id)
    assert [m.project_id for m in marks(db, reader="planner")] == [world["p2_project"].id]
    assert {m.fact_id for m in marks(db, reader="support-bot")} == \
        {world["u1"][0].id, world["u1"][1].id, world["u2"][0].id}


@pytest.mark.parametrize("erase", [True, False], ids=["erased", "moved"])
def test_a_projects_deletion_takes_its_marks(client, db, tenant, world, erase):
    """Erased or moved to the Default project, a deleted project's marks go
    with it: they are its readers'."""
    p2 = world["p2_project"]
    client.get("/v1/facts", params={"reader": "support-bot"},
               headers=_head(_key(db, tenant, project_id=p2.id)))
    _mark_everything(client, db, tenant)
    assert marks(db, project_id=p2.id)
    project_service.delete_project(db, tenant.id, p2, erase=erase)
    db.commit()
    assert marks(db, project_id=p2.id) == []
    assert len(marks(db)) == 4


def test_a_deleted_fact_takes_its_marks(client, db, tenant, world):
    _mark_everything(client, db, tenant)
    gone = world["u2"][0]
    db.delete(gone)
    db.commit()
    assert marks(db, fact_id=gone.id) == []
    assert len(marks(db)) == 3


def test_the_purge(client, db, tenant, world):
    """Not read again for READ_MARKS_RETENTION_DAYS: gone; a change shown
    (`notified_at`): gone at the next purge, whatever the setting."""
    _mark_everything(client, db, tenant)
    old, recent, told, kept = (world["u1"][0], world["u1"][1], world["u2"][0], world["agent"][0])
    db.execute(text("UPDATE agent_read_marks SET last_read_at = now() - interval '91 days' "
                    "WHERE fact_id = :f"), {"f": old.id})
    db.execute(text("UPDATE agent_read_marks SET last_read_at = now() - interval '89 days' "
                    "WHERE fact_id = :f"), {"f": recent.id})
    db.execute(text("UPDATE agent_read_marks SET notified_at = now() WHERE fact_id = :f"),
               {"f": told.id})
    db.commit()
    assert read_marks.purge_expired(db, 0) == 1, "0 keeps them, but not a change already told"
    assert {m.fact_id for m in marks(db)} == {old.id, recent.id, kept.id}
    assert read_marks.purge_expired(db, 90) == 1
    assert {m.fact_id for m in marks(db)} == {recent.id, kept.id}


def test_the_default_is_ninety_days():
    from korely_agent.config import AgentSettings

    assert AgentSettings.model_fields["read_marks_retention_days"].default == 90
    assert read_marks.DEFAULT_RETENTION_DAYS == 90
    assert "90 days" in read_marks.READER


# ── a mark never fails the read ────────────────────────────────────────────

def _until_a_mark_waits(db, seconds=10.0):
    """Whether another session of this database waits on a lock within
    `seconds`: the mark, blocked by the transaction the test holds open."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        db.rollback()  # each poll a fresh view of pg_stat_activity
        n = db.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = "
                            "current_database() AND wait_event_type = 'Lock'")).scalar()
        if n:
            return True
        time.sleep(0.01)
    return False


def _marking(tenant_id, project_id, ids):
    t = threading.Thread(target=read_marks.mark,
                         args=(tenant_id, project_id, "support-bot", ids))
    t.start()
    return t


@pytest.fixture
def patient(monkeypatch):
    """A mark that waits long enough to see the race, not its deadline."""
    from korely_agent.core import threads

    monkeypatch.setattr(threads, "MARK_LOCK_WAIT_SECONDS", 10)


def test_a_fact_deleted_while_it_is_marked_leaves_the_others_marked(db, tenant, default, world,
                                                                     patient):
    """Two real sessions: one deletes a fact the read handed over and has
    not committed, the mark waits for it, and once the deletion commits the
    fact is skipped and the others are marked. The foreign key's own check
    refused the whole statement, every other fact went unmarked and /health
    read degraded (review of piece 1)."""
    from korely_agent.db import get_sessionmaker

    victim, others = world["u1"][0], [world["u1"][1], world["u2"][0], world["agent"][0]]
    eraser = get_sessionmaker()()
    try:
        eraser.execute(text("DELETE FROM agent_facts WHERE id = :i"), {"i": victim.id})
        t = _marking(tenant.id, default.id, [victim.id] + [f.id for f in others])
        assert _until_a_mark_waits(db), "the mark did not wait for the deletion"
        eraser.commit()
        t.join(30)
    finally:
        eraser.close()
    assert {m.fact_id for m in marks(db)} == {f.id for f in others}
    state = bookkeeping.state()["read_marks"]
    assert state["state"] == "working" and "lost" not in state, state
    assert state["dropped_with_what_they_name"] == 1, state


def test_a_project_deleted_while_it_is_marked_marks_nothing_and_fails_nothing(db, tenant, world,
                                                                              patient):
    """The project's row is locked before the facts, as a deletion takes
    them: a project deleted meanwhile is waited for, and nothing is marked in
    it, where the foreign key refused the statement."""
    from korely_agent.db import get_sessionmaker

    p2 = world["p2_project"]
    eraser = get_sessionmaker()()
    try:
        eraser.execute(text("DELETE FROM agent_projects WHERE id = :p"), {"p": p2.id})
        t = _marking(tenant.id, p2.id, [world["p2"][0].id])
        assert _until_a_mark_waits(db)
        eraser.commit()
        t.join(30)
    finally:
        eraser.close()
    assert marks(db) == []
    state = bookkeeping.state()["read_marks"]
    assert state["state"] == "working" and state["dropped_with_what_they_name"] == 1, state


def _gave_way(client, n):
    """The mark gave way to a lock: counted apart, no failure, /health ok."""
    state = bookkeeping.state()["read_marks"]
    assert state["state"] != "failing" and "lost" not in state, state
    assert state["gave_way"] == n, state
    health = client.get("/health").json()
    assert health["records"]["read_marks"]["gave_way"] == n
    assert health["status"] == "ok", health


@pytest.mark.serial  # a fifth of a second, measured: no parallel neighbours
@pytest.mark.parametrize("path, params", [
    ("/v1/facts", {"user_id": "u1"}),
    ("/v1/context", {"query": "Where does Maria live?", "user_id": "u1"})], ids=["facts", "context"])
def test_a_lock_on_the_table_costs_the_read_a_fifth_of_a_second(client, db, tenant, world, path,
                                                                params, monkeypatch):
    """A lock held on the marks' table (a migration) made every read with a
    reader wait the request's 5 s: the mark, and the block's look at the
    changes, wait 0.2 s each and give way, and the read answers as without
    a reader. Giving way is no failure: /health stays ok (review of piece 1,
    8 Nov 2026: it read degraded).

    Each step gives way at its lock's deadline, `lock_not_available`
    (SQLSTATE 55P03), never cut by its statement's (57014): a mark whose
    lock wait came to pass its statement deadline is caught by the code,
    whatever the machine. The clock tells apart what is left: the steps
    wait a fifth of a second each (0.21 s in all for the facts, one step;
    0.45 s for the context, two; three runs on a laptop on 23 Nov 2026), and
    a step on the request's own deadlines (`threads.LOCK_WAIT_SECONDS`, 5 s)
    would take 5 s at least. The bound was 1 s until 1.0.35, when it was
    also what caught the first kind: the context read took 2.33 s once on
    the public CI of 23 Nov 2026, in one process at the end of the whole
    suite on a shared runner, and passed when run again; that pause was not
    reproduced on the laptop. 3 s tells 0.45 s from 5 s, and the assertion
    beside it fails if the constants come to meet it."""
    from korely_agent.core import threads
    from korely_agent.db import get_sessionmaker

    codes = []
    deadline_passed = threads.deadline_passed

    def the_code(e):
        codes.append(getattr(getattr(e, "orig", None), "pgcode", None))
        return deadline_passed(e)

    monkeypatch.setattr(threads, "deadline_passed", the_code)
    full = _key(db, tenant)
    without = client.get(path, params=params, headers=_head(full))
    holder = get_sessionmaker()()
    try:
        holder.execute(text("LOCK TABLE agent_read_marks IN ACCESS EXCLUSIVE MODE"))
        t0 = time.monotonic()
        r = client.get(path, params={**params, "reader": "support-bot"}, headers=_head(full))
        waited = time.monotonic() - t0
    finally:
        holder.rollback()
        holder.close()
    answered = r.json()
    if path == "/v1/context":
        assert (answered.pop("changed"), answered.pop("changed_more")) == ([], None), \
            "the changes could not be read: none told, and they wait"
    assert r.status_code == 200 and answered == without.json()
    assert codes == ["55P03"] * (2 if path == "/v1/context" else 1), codes
    assert threads.MARK_LOCK_WAIT_SECONDS * 2 < 3.0 < threads.LOCK_WAIT_SECONDS
    assert waited < 3.0, f"the read waited {waited:.2f} s for its bookkeeping"
    handed = (answered["facts"] if path == "/v1/facts"
              else [s for s in answered["sources"] if s.startswith("fct_")])
    assert handed
    _gave_way(client, len(handed))
    assert marks(db) == []


def _hold(fact_id, how, seconds, until=None):
    """Another transaction holds the fact's row for `seconds`, then lets go:
    a deletion not yet committed, or the late judgement of a vector, which
    locks the facts FOR UPDATE until it commits (services/vectors.py).
    `until` (a threading.Event): held until it is set instead, at most
    `seconds`, so a test that only asks what the read did while the row was
    held does not depend on how long the read takes on a loaded machine."""
    from korely_agent.db import get_sessionmaker

    holder = get_sessionmaker()()
    if how == "deletion":
        holder.execute(text("DELETE FROM agent_facts WHERE id = :i"), {"i": fact_id})
    else:
        holder.execute(text("SELECT id FROM agent_facts WHERE id = :i FOR UPDATE"),
                       {"i": fact_id})

    def release():
        if until is None:
            time.sleep(seconds)
        else:
            until.wait(seconds)
        holder.rollback()
        holder.close()

    t = threading.Thread(target=release)
    t.start()
    return t


@pytest.mark.serial  # a fifth of a second against a second, measured
@pytest.mark.parametrize("how", ["deletion", "late judgement"])
def test_a_fact_held_for_a_second_costs_only_its_own_mark(client, db, tenant, default, world, how):
    """With the constants as they are (review of piece 1: the test of the
    race waited 10 s, the product waits 0.2 s): a fact held for a second by
    another transaction gives way after a fifth of a second, alone; the
    read's other facts are marked at once, and /health stays ok. Before, the
    one statement gave up and no fact of the read was marked."""
    victim, others = world["u1"][0], [world["u1"][1], world["u2"][0], world["agent"][0]]
    held = _hold(victim.id, how, 1.0)
    try:
        t0 = time.monotonic()
        read_marks.mark(tenant.id, default.id, "support-bot", [victim.id] + [f.id for f in others])
        waited = time.monotonic() - t0
    finally:
        held.join(10)
    assert {m.fact_id for m in marks(db)} == {f.id for f in others}
    assert waited < 0.8, f"the mark waited {waited:.2f} s"
    _gave_way(client, 1)


@pytest.mark.serial
def test_a_read_while_a_fact_is_held_answers_and_health_stays_ok(client, db, tenant, world):
    """The same through REST: the read answers, three of its four facts are
    marked, /health reads ok. The row is held until the read has answered,
    not for a second: on a loaded machine the read reached its mark after
    the second had passed, and marked the fourth fact too (2 runs in 8,
    the sixth review of piece 3, 9 Nov 2026)."""
    full = _key(db, tenant)
    victim = world["u1"][0]
    answered = threading.Event()
    held = _hold(victim.id, "deletion", 60.0, until=answered)
    try:
        r = client.get("/v1/facts", params={"reader": "support-bot"}, headers=_head(full))
    finally:
        answered.set()
        held.join(10)
    assert r.status_code == 200 and len(r.json()["facts"]) == 4
    assert {m.fact_id for m in marks(db)} == {world["u1"][1].id, world["u2"][0].id,
                                              world["agent"][0].id}
    _gave_way(client, 1)


@pytest.mark.serial  # a second of a statement, measured
def test_a_slow_mark_gives_up_after_a_second(client, db, tenant, default, world):
    """A mark's statement waits at most a second (core/threads.
    MARK_STATEMENT_SECONDS), then gives way: a trigger that sleeps three
    seconds on the marks' table costs the read about one (review of piece 1:
    no test held the statement's deadline)."""
    db.execute(text("""
        CREATE OR REPLACE FUNCTION neo_slow_mark() RETURNS trigger AS $$
        BEGIN PERFORM pg_sleep(3); RETURN NEW; END $$ LANGUAGE plpgsql"""))
    db.execute(text("CREATE TRIGGER neo_slow_mark BEFORE INSERT ON agent_read_marks "
                    "FOR EACH ROW EXECUTE FUNCTION neo_slow_mark()"))
    db.commit()
    try:
        t0 = time.monotonic()
        read_marks.mark(tenant.id, default.id, "support-bot", [world["u1"][0].id])
        waited = time.monotonic() - t0
    finally:
        db.execute(text("DROP TRIGGER IF EXISTS neo_slow_mark ON agent_read_marks"))
        db.execute(text("DROP FUNCTION IF EXISTS neo_slow_mark()"))
        db.commit()
    assert 0.9 < waited < 2.5, f"the mark's statement ran {waited:.2f} s"
    assert marks(db) == []
    _gave_way(client, 1)

SECRET = "zz-secret-reader-7f2c"


@pytest.fixture
def marks_fail(monkeypatch):
    """The own session refuses the marks' insert, with the reader's name in
    the driver's message, and nothing else."""
    from sqlalchemy.exc import OperationalError

    from korely_agent import db as db_package

    real = db_package.get_own_sessionmaker

    def failing():
        maker = real()

        def make():
            s = maker()
            execute = s.execute

            def refused(stmt, *a, **k):
                if getattr(getattr(stmt, "table", None), "name", None) == "agent_read_marks":
                    raise OperationalError(f"INSERT ... {SECRET}", {"reader": SECRET},
                                           Exception(f"could not write {SECRET}"))
                return execute(stmt, *a, **k)

            s.execute = refused
            return s
        return make

    monkeypatch.setattr(db_package, "get_own_sessionmaker", failing)


@pytest.mark.parametrize("door", sorted(DOORS))
def test_a_mark_that_fails_does_not_fail_the_read(client, db, tenant, world, door, marks_fail,
                                                  caplog):
    full = _key(db, tenant)
    (path, params), (tool, args) = DOORS[door]
    without = client.get(path, params=params, headers=_head(full))
    with caplog.at_level(logging.ERROR):
        r = client.get(path, params={**params, "reader": SECRET}, headers=_head(full))
        said = mcp(full, tool, {**args, "reader": SECRET})
    answered = r.json()
    if door == "context":
        assert (answered.pop("changed"), answered.pop("changed_more")) == ([], 0)
    assert r.status_code == 200 and answered == without.json()
    assert said == mcp(full, tool, args)
    assert marks(db) == []
    state = bookkeeping.state()["read_marks"]
    assert state["state"] == "failing" and state["lost"] == 2 * len(world["u1"]), state
    assert "[read_marks] could not mark" in caplog.text
    assert SECRET not in caplog.text, "the log names the reader or quotes the driver"
