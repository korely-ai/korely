"""`known_as_of`: the facts as the store knew them at a moment (11 Oct 2026).

Two clocks, as in Graphiti: when a fact was true in the world (`valid_from`,
`invalid_at`, read by `as_of`) and when the store knew it (`created_at`, and
`closed_known_at`, filled by the database when a fact is closed). The case a
reader asked about: an agent learns in October that a contact moved in
September. `as_of` 20 September answers the new address (what was true that
day, as the store knows it now); `known_as_of` 20 September the old one (what
the store knew that day).
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import keys as key_service
from korely_agent.services import vectors
from korely_agent.services.facts import service as facts_service
from korely_agent.services.ids import fact_public_id, mem_public_id
from tests.conftest import seeded_project_id
from korely_agent.services.sentinels import EVERY_PROJECT


UTC = timezone.utc
JAN1 = datetime(2026, 1, 1, tzinfo=UTC)          # Maria lives in Milan since
AUG1 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)    # ... and the store learns it
SEP1 = datetime(2026, 9, 1, tzinfo=UTC)          # she moves to Turin
SEP20 = datetime(2026, 9, 20, tzinfo=UTC)
OCT5 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)   # the store learns the move
OCT10 = datetime(2026, 10, 10, tzinfo=UTC)


def _fact(db, tenant, project, memory, obj, *, learned, valid_from):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project, end_user_id="maria",
                  subject_label="maria", subject_type="self", predicate_raw="lives_in",
                  predicate_canonical="lives_in", object_label=obj, confidence=0.9,
                  valid_from=valid_from, created_at=learned, embedding=[0.1] * EMBEDDING_DIM,
                  source_memory_id=memory.id, source_memory_ids=[str(memory.id)])
    db.add(f)
    db.flush()
    return f


def _memory(db, tenant, project, content, *, received):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project, end_user_id="maria",
                    content=content, embedding=[0.1] * EMBEDDING_DIM,
                    embedding_status=vectors.DONE, graph_sync_status="done",
                    created_at=received, event_time=received)
    db.add(m)
    db.flush()
    return m


@pytest.fixture
def move(db, tenant):
    """Milan learned on 1 August; the move to Turin of 1 September learned on
    5 October, which closes Milan on 1 September, a closure learned on 5
    October (set in the same statement, so the trigger leaves it)."""
    project = seeded_project_id(db, tenant.id)
    old = _memory(db, tenant, project, "user: I live in Milan.", received=AUG1)
    new = _memory(db, tenant, project, "user: I moved to Turin at the start of September.",
                  received=OCT5)
    milan = _fact(db, tenant, project, old, "Milan", learned=AUG1, valid_from=JAN1)
    turin = _fact(db, tenant, project, new, "Turin", learned=OCT5, valid_from=SEP1)
    db.commit()
    milan.invalid_at, milan.invalidated_by_id, milan.closed_known_at = SEP1, turin.id, OCT5
    db.commit()
    return {"project": project, "milan": milan, "turin": turin, "old": old, "new": new}


def objects(db, tenant, **kw):
    kw.setdefault("project_id", EVERY_PROJECT)
    rows, _ = facts_service.retrieve_agent_facts(db, tenant.id, end_user_id="maria", **kw)
    return sorted(f.object_label for f in rows)


# ── the database fills it ───────────────────────────────────────────────────

def _closed_known(db, fid):
    return db.execute(text("SELECT closed_known_at, created_at FROM agent_facts WHERE id = :id"),
                      {"id": fid}).one()


def test_an_open_fact_has_none(db, tenant, move):
    assert _closed_known(db, move["turin"].id)[0] is None


def test_a_fact_born_closed_learned_its_end_with_it(db, tenant):
    project = seeded_project_id(db, tenant.id)
    m = _memory(db, tenant, project, "user: I used to live in Rome.", received=OCT5)
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project, end_user_id="maria",
                  subject_label="maria", predicate_raw="lives_in", predicate_canonical="lives_in",
                  object_label="Rome", confidence=0.9, valid_from=JAN1, invalid_at=SEP1,
                  tense="past", source_memory_id=m.id)
    db.add(f)
    db.commit()
    known, created = _closed_known(db, f.id)
    assert known is not None and known == created, "the same transaction, the same clock"


def test_a_closure_written_later_is_learned_then(db, tenant, move):
    turin = move["turin"]
    before = datetime.now(UTC) - timedelta(seconds=1)
    turin.invalid_at = OCT10
    db.commit()
    known, _ = _closed_known(db, turin.id)
    assert known >= before, "learned when it was written, not on the end date"


def test_a_bulk_closure_is_seen_too(db, tenant, move):
    """A forget of a whole end user is one UPDATE that no ORM event sees."""
    db.query(AgentFact).filter(AgentFact.id == move["turin"].id).update(
        {AgentFact.invalid_at: OCT10}, synchronize_session=False)
    db.commit()
    assert _closed_known(db, move["turin"].id)[0] is not None


def test_a_closure_undone_is_unlearned(db, tenant, move):
    milan = move["milan"]
    milan.invalid_at = None
    milan.invalidated_by_id = None
    db.commit()
    assert _closed_known(db, milan.id)[0] is None


def test_a_value_written_with_the_closure_stands(db, tenant, move):
    """A restore or a backfill writes the closure and when it was learned in
    one statement; the trigger leaves that value."""
    assert _closed_known(db, move["milan"].id)[0] == OCT5


def test_another_change_leaves_it_alone(db, tenant, move):
    milan = move["milan"]
    milan.observation_count = 3
    db.commit()
    assert _closed_known(db, milan.id)[0] == OCT5


def test_an_end_is_never_learned_before_the_fact(db, tenant, engine):
    """A transaction opens (a write waiting on its model), another worker
    writes a fact and commits, the first transaction closes that fact: the
    end read as learned at the first transaction's start, before the fact
    itself was learned (audit of 17 Oct 2026)."""
    import time

    from sqlalchemy.orm import Session

    project = seeded_project_id(db, tenant.id)
    m = _memory(db, tenant, project, "user: I live in Rome.", received=OCT5)
    db.commit()
    closer = Session(bind=engine)
    try:
        closer.execute(text("SELECT now()"))   # its transaction, and its clock, start here
        time.sleep(0.05)
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project,
                      end_user_id="maria", subject_label="maria", predicate_raw="lives_in",
                      predicate_canonical="lives_in", object_label="Rome", confidence=0.9,
                      valid_from=JAN1, source_memory_id=m.id)
        db.add(f)
        db.commit()
        closer.execute(text("UPDATE agent_facts SET invalid_at = :at WHERE id = :id"),
                       {"at": SEP1, "id": f.id})
        closer.commit()
    finally:
        closer.close()
    known, created = _closed_known(db, f.id)
    assert known >= created, "the end was learned before the fact"


# ── the two clocks ──────────────────────────────────────────────────────────

def test_now_turin(db, tenant, move):
    assert objects(db, tenant) == ["Turin"]


def test_as_of_september_turin_as_known_now(db, tenant, move):
    assert objects(db, tenant, as_of=SEP20) == ["Turin"]


def test_known_as_of_september_milan(db, tenant, move):
    """What the store believed on 20 September: the move was told on 5 October."""
    assert objects(db, tenant, known_as_of=SEP20) == ["Milan"]


def test_known_as_of_october_turin(db, tenant, move):
    assert objects(db, tenant, known_as_of=OCT10) == ["Turin"]


def test_known_in_october_about_august_milan(db, tenant, move):
    assert objects(db, tenant, known_as_of=OCT10, as_of=AUG1 + timedelta(days=14)) == ["Milan"]


def test_known_in_september_about_november_still_milan(db, tenant, move):
    """In September the store had no reason to think Milan would end."""
    assert objects(db, tenant, known_as_of=SEP20, as_of=datetime(2026, 11, 1, tzinfo=UTC)) == ["Milan"]


def test_nothing_before_it_was_learned(db, tenant, move):
    assert objects(db, tenant, known_as_of=datetime(2026, 7, 1, tzinfo=UTC)) == []


def test_with_the_ended_ones_every_fact_learned_by_then(db, tenant, move):
    assert objects(db, tenant, known_as_of=SEP20, include_invalidated=True) == ["Milan"]
    assert objects(db, tenant, known_as_of=OCT10, include_invalidated=True) == ["Milan", "Turin"]


def test_each_fact_reads_as_it_was_known(db, tenant, move):
    milan = move["milan"]
    then = facts_service.serialize_fact(milan, SEP20)
    assert then["invalid_at"] is None and then["invalidated_by"] is None
    assert then["closed_known_at"] is None
    now = facts_service.serialize_fact(milan)
    assert now["invalid_at"].startswith("2026-09-01")
    assert now["invalidated_by"] == fact_public_id(move["turin"].id)
    assert now["closed_known_at"].startswith("2026-10-05")
    assert facts_service.serialize_fact(milan, OCT10)["invalid_at"] == now["invalid_at"]


# ── the API ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient

    from korely_agent.db import get_db
    from korely_agent.main import create_app
    from korely_agent.services import context as ctx

    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def head(db, tenant, move):
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name="agent",
                                          project_id=move["project"], expires_in_days=None)
    return {"Authorization": f"Bearer {full}"}


def test_the_facts_route(client, head):
    def ask(**params):
        r = client.get("/v1/facts", params={"user_id": "maria", **params}, headers=head)
        assert r.status_code == 200, r.text
        return [f["object"] for f in r.json()["facts"]]

    assert ask(as_of="2026-09-20") == ["Turin"]
    assert ask(known_as_of="2026-09-20") == ["Milan"]
    r = client.get("/v1/facts", params={"known_as_of": "last week"}, headers=head)
    assert r.status_code == 422 and "known_as_of" in r.text


def test_the_profile_route(client, head):
    r = client.get("/v1/profile", params={"user_id": "maria", "known_as_of": "2026-09-20"},
                   headers=head)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["known_as_of"] == "2026-09-20"
    assert [f["object"] for f in body["facts"]] == ["Milan"]


def test_the_read_is_audited_with_its_moment(db, tenant, client, head):
    from korely_agent.db.models import AuditEvent

    client.get("/v1/facts", params={"user_id": "maria", "known_as_of": "2026-09-20"}, headers=head)
    db.expire_all()
    row = (db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id)
           .order_by(AuditEvent.ts.desc()).first())
    assert (row.meta or {}).get("known_as_of") == "2026-09-20"


def test_the_context_as_it_could_be_built_then(client, head, move):
    def block(**params):
        r = client.get("/v1/context", params={"query": "Where does Maria live?",
                                              "user_id": "maria", "token_budget": 2000,
                                              **params}, headers=head)
        assert r.status_code == 200, r.text
        return r.json()

    now = block()
    assert "Turin" in now["context"]
    then = block(known_as_of="2026-09-20")
    assert "Milan" in then["context"] and "Turin" not in then["context"]
    assert fact_public_id(move["turin"].id) not in then["sources"]
    assert mem_public_id(move["new"].id) not in then["sources"], "received on 5 October"


# ── the MCP ─────────────────────────────────────────────────────────────────

def _mcp(key, tool, args):
    from mcp.server.fastmcp.exceptions import ToolError

    from korely_agent.mcp import server as mcp_server

    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(key) is not None
        try:
            return str(await mcp_server._get_agent_mcp().call_tool(tool, args))
        except ToolError as e:
            return f"ToolError: {e}"
    return asyncio.run(go())


def test_the_mcp_facts_tool(db, tenant, move):
    _r, key = key_service.create_api_key(db, tenant_id=tenant.id, name="mcp",
                                         project_id=move["project"], expires_in_days=None)
    then = _mcp(key, "korely_get_facts", {"user_id": "maria", "known_as_of": "2026-09-20"})
    assert "Milan" in then and "Turin" not in then and "as known on 2026-09-20" in then
    wrong = _mcp(key, "korely_get_facts", {"user_id": "maria", "known_as_of": "soon"})
    assert "known_as_of must be an ISO date" in wrong


def test_the_spec_lists_what_a_fact_carries():
    """The facts of /v1/facts and /v1/profile are typed (0.1.27): the
    OpenAPI document names every field, `closed_known_at` included."""
    from korely_agent.main import create_app

    schemas = create_app().openapi()["components"]["schemas"]
    fact = schemas["Fact"]["properties"]
    assert {"closed_known_at", "created_at", "valid_from", "invalid_at", "tense"} <= set(fact)
    assert schemas["FactsResponse"]["properties"]["facts"]["items"] == {"$ref": "#/components/schemas/Fact"}


def test_a_fact_read_is_the_same_answer(client, head, move):
    """Typed, the answer keeps every key serialize_fact writes."""
    r = client.get("/v1/facts", params={"user_id": "maria", "include_invalidated": "true"}, headers=head)
    assert r.status_code == 200, r.text
    milan = next(f for f in r.json()["facts"] if f["object"] == "Milan")
    assert set(milan) == set(facts_service.serialize_fact(move["milan"]))
    assert milan["closed_known_at"].startswith("2026-10-05")
