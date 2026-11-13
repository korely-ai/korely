"""What changed since a reader last read (services/facts/changes.py, 1.0.15).

A change is a fact a reader was handed as true by a read of now that has
ended since, told once. These hold its rule (not "closed_known_at is set": a
fact with an end to come is true now), the successor down a chain, the four
origins, what gives no notice (a memory forgotten, a person erased, a fact
true again), the order, the count, and the walls of the key that asks now:
the account, the project, the end user, the successor it may not read.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory, AgentReadMark, Tenant
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service
from korely_agent.services import read_marks, vectors
from korely_agent.services.facts import changes
from korely_agent.services.ids import fact_public_id, mem_public_id
from korely_agent.services.sentinels import EVERY_END_USER, EndUsers

VECTOR = [0.1] * EMBEDDING_DIM
BOT = "support-bot"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _vector(monkeypatch):
    from korely_agent.services import context as ctx
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


def fact(db, tenant_id, project_id, *, user="u1", agent=None, subject="Maria", obj="Rome",
         model="gemini-test", memory=None):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant_id, project_id=project_id, end_user_id=user,
                  agent_id=agent, subject_label=subject, predicate_raw="lives in",
                  predicate_canonical="lives_in", object_label=obj, valid_from=START,
                  model_used=model, source_memory_id=getattr(memory, "id", None),
                  embedding=VECTOR, embedding_status=vectors.DONE)
    db.add(f)
    db.commit()
    return f


def closed(db, f, by=None, at=None):
    """`f` ended at `at` (now), replaced by `by`, as the judge closes it."""
    f.invalid_at = at or datetime.now(timezone.utc)
    f.invalidated_by_id = getattr(by, "id", None)
    db.commit()
    return f


def read(tenant_id, project_id, *facts):
    read_marks.mark(tenant_id, project_id, BOT, [f.id for f in facts])


def told(db, tenant, project, end_users=EVERY_END_USER, **kw):
    db.expire_all()
    return changes.changed_since(db, tenant.id, project.id, BOT, end_users, **kw)


# ── the rule ────────────────────────────────────────────────────────────────

def test_a_fact_read_and_replaced_since_is_a_change(db, tenant, default):
    mem = AgentMemory(tenant_id=tenant.id, project_id=default.id, end_user_id="u1",
                      agent_id="planner", content="Maria moved to Milan")
    db.add(mem)
    db.commit()
    old = fact(db, tenant.id, default.id)
    read(tenant.id, default.id, old)
    new = fact(db, tenant.id, default.id, obj="Milan", agent="planner", memory=mem)
    closed(db, old, by=new)
    (c,), more = told(db, tenant, default)
    assert more == 0
    assert c["fact_id"] == fact_public_id(old.id)
    assert c["was"].startswith("Maria lives in Rome") and "\n" not in c["was"]
    assert c["now"] == {"fact_id": fact_public_id(new.id), "text": c["now"]["text"]}
    assert c["now"]["text"].startswith("Maria lives in Milan")
    assert c["origin"] == changes.READ_FROM_MEMORY
    assert c["source_memory_id"] == mem_public_id(mem.id) and c["by_agent"] == "planner"
    assert c["ended_at"] and c["known_at"], c


def test_an_end_to_come_is_no_change_until_it_comes(db, tenant, default):
    """`closed_known_at` is set the day the end is learned, an end dated
    ahead included: the fact is true now, and no change (review of piece 1)."""
    f = fact(db, tenant.id, default.id)
    read(tenant.id, default.id, f)
    closed(db, f, at=datetime.now(timezone.utc) + timedelta(days=30))
    db.refresh(f)
    assert f.closed_known_at is not None
    assert told(db, tenant, default) == ([], 0)
    closed(db, f, at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert [c["fact_id"] for c in told(db, tenant, default)[0]] == [fact_public_id(f.id)]


def test_a_fact_true_again_is_no_change(db, tenant, default):
    f = fact(db, tenant.id, default.id)
    read(tenant.id, default.id, f)
    closed(db, f)
    f.invalid_at = None
    db.commit()
    assert told(db, tenant, default) == ([], 0)


def test_a_fact_never_read_is_no_change_and_another_readers_are_not_mine(db, tenant, default):
    f, g = fact(db, tenant.id, default.id), fact(db, tenant.id, default.id, obj="Acme")
    read_marks.mark(tenant.id, default.id, "billing-bot", [f.id])
    closed(db, f)
    closed(db, g)
    assert told(db, tenant, default) == ([], 0)


def test_down_a_chain_the_one_that_holds_now(db, tenant, default):
    a = fact(db, tenant.id, default.id)
    read(tenant.id, default.id, a)
    b = fact(db, tenant.id, default.id, obj="Milan")
    closed(db, a, by=b)
    c = fact(db, tenant.id, default.id, obj="Turin")
    closed(db, b, by=c)
    (change,), _ = told(db, tenant, default)
    assert change["now"]["fact_id"] == fact_public_id(c.id)


def test_told_once(db, tenant, default):
    f = fact(db, tenant.id, default.id)
    read(tenant.id, default.id, f)
    closed(db, f)
    assert len(told(db, tenant, default, consume=False)[0]) == 1
    assert len(told(db, tenant, default)[0]) == 1, "looking without consuming leaves it"
    assert told(db, tenant, default) == ([], 0)
    db.expire_all()
    assert db.query(AgentReadMark).one().notified_at is not None


def test_newest_first_and_the_rest_counted(db, tenant, default):
    facts = [fact(db, tenant.id, default.id, obj=f"place {i}") for i in range(5)]
    read(tenant.id, default.id, *facts)
    for i, f in enumerate(facts):
        closed(db, f)
        f.closed_known_at = START + timedelta(days=i)
        db.commit()
    shown, more = told(db, tenant, default, limit=3)
    assert [c["fact_id"] for c in shown] == [fact_public_id(f.id) for f in facts[::-1][:3]]
    assert more == 2


# ── the origins ─────────────────────────────────────────────────────────────

def _key(db, tenant, **kw):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id,
                                            name=f"k-{uuid.uuid4().hex[:6]}", **kw)
    return {"Authorization": f"Bearer {full}"}


def test_a_correction_and_a_forget_through_their_doors(client, db, tenant, default):
    head = _key(db, tenant)
    ids = [client.post("/v1/facts", headers=head, json={
        "subject": "Maria", "predicate": p, "object": o, "user_id": "u1"}).json()["id"]
        for p, o in (("lives_in", "Rome"), ("works_at", "Acme"))]
    assert client.get("/v1/facts", params={"reader": BOT}, headers=head).status_code == 200
    r = client.patch(f"/v1/facts/{ids[0]}", headers=head, json={"object": "Milan"})
    assert r.status_code == 200, r.text
    assert client.post(f"/v1/facts/{ids[1]}/forget", headers=head).status_code == 200
    by_id = {c["fact_id"]: c for c in told(db, tenant, default)[0]}
    assert by_id[ids[0]]["origin"] == changes.CORRECTED
    assert "Milan" in by_id[ids[0]]["now"]["text"]
    assert by_id[ids[1]]["origin"] == changes.FORGOTTEN and by_id[ids[1]]["now"] is None


def test_a_fact_a_caller_wrote_closed_it(db, tenant, default):
    old = fact(db, tenant.id, default.id)
    read(tenant.id, default.id, old)
    closed(db, old, by=fact(db, tenant.id, default.id, obj="Milan", model="direct"))
    assert told(db, tenant, default)[0][0]["origin"] == changes.STATED


def test_ended_in_place(db, tenant, default):
    """A past statement ends the fact in place, no successor, and writes on
    the row only its end: whoever wrote the fact, a memory's statement or a
    caller's (POST /v1/facts, tense past) may have ended it, so neither is
    told as explicit (review of piece 2: a caller's fact read `stated`)."""
    mem = AgentMemory(tenant_id=tenant.id, project_id=default.id, end_user_id="u1",
                      content="Maria lived in Rome")
    db.add(mem)
    db.commit()
    from_memory = fact(db, tenant.id, default.id, memory=mem)
    by_a_caller = fact(db, tenant.id, default.id, obj="Acme", model="direct")
    read(tenant.id, default.id, from_memory, by_a_caller)
    closed(db, from_memory)
    closed(db, by_a_caller)
    by_id = {c["fact_id"]: c for c in told(db, tenant, default)[0]}
    assert by_id[fact_public_id(from_memory.id)]["origin"] == changes.READ_FROM_MEMORY
    assert by_id[fact_public_id(by_a_caller.id)]["origin"] == changes.READ_FROM_MEMORY
    assert all(c["now"] is None for c in by_id.values())


@pytest.mark.parametrize("whose", ["an_end_users", "an_agents"])
def test_a_past_fact_posted_by_hand_is_read_from_memory_through_the_doors(
        client, db, tenant, default, whose):
    """Through the doors: a fact posted by hand, read by a named reader, then
    posted again with tense 'past', which ends it in place. Its record, of
    scope 'past' (facts/given_back.py, a past statement and not a forget),
    keeps a deletion from giving the fact back; the notice reads it for what
    it is, an end in place, `read_from_memory` like a memory's (the guide),
    never `forgotten`. In an agent's memory with no end user it is told only
    with `changes=all` (review of the merge with 1.0.14, 10 Nov 2026: it was
    read as a forget, explicit, and told by default)."""
    head = _key(db, tenant)
    who = {"user_id": "u1"} if whose == "an_end_users" else {"agent_id": "planner"}
    said = {"subject": "Maria", "predicate": "works_at", "object": "Acme", **who}
    fid = client.post("/v1/facts", headers=head, json=said).json()["id"]
    assert client.get("/v1/facts", params={"reader": BOT, **who}, headers=head).status_code == 200
    r = client.post("/v1/facts", headers=head, json={**said, "tense": "past"})
    assert r.status_code == 201, r.text
    db.expire_all()
    row = db.get(AgentFact, uuid.UUID(fid[len("fct_"):]))
    assert row.invalid_at is not None and row.invalidated_by_id is None, "ended in place"

    def changed(**params):
        r = client.get("/v1/context", params={"query": "Where does Maria work?", "reader": BOT,
                                              **who, **params}, headers=head)
        assert r.status_code == 200, r.text
        return [(c["fact_id"], c["origin"], c["now"]) for c in r.json()["changed"]]

    if whose == "an_agents":
        assert changed() == [], "an end in place is not an explicit closure"
    assert changed(changes="all") == [(fid, changes.READ_FROM_MEMORY, None)]


# ── what gives no notice ───────────────────────────────────────────────────

def test_a_memory_forgotten_gives_no_notice(db, tenant, default):
    from korely_agent.services.facts import erasure

    mem = AgentMemory(tenant_id=tenant.id, project_id=default.id, end_user_id="u1",
                      content="Maria lives in Rome", graph_sync_status="done")
    db.add(mem)
    db.commit()
    f = fact(db, tenant.id, default.id, memory=mem)
    f.source_memory_ids = [str(mem.id)]
    db.commit()
    read(tenant.id, default.id, f)
    assert erasure.forget_memory(db, tenant.id, mem.id, project_id=default.id)
    db.refresh(f)
    assert f.invalid_at is not None, "the forget closed the fact it alone stated"
    assert told(db, tenant, default) == ([], 0)


def test_an_erased_person_gives_no_notice(db, tenant, default):
    from korely_agent.services.facts import service

    f = fact(db, tenant.id, default.id)
    read(tenant.id, default.id, f)
    closed(db, f)
    service.forget_end_user(db, tenant.id, "u1", project_id=default.id)
    assert told(db, tenant, default) == ([], 0)


# ── the walls, those of the key that asks now ───────────────────────────────

def test_the_account_and_the_project(db, tenant, default):
    f = fact(db, tenant.id, default.id)
    read(tenant.id, default.id, f)
    closed(db, f)
    other = Tenant(id=uuid.uuid4(), name="Other")
    db.add(other)
    db.commit()
    assert told(db, other, default) == ([], 0)
    p2 = project_service.create_project(db, tenant_id=tenant.id, name="P2")
    assert told(db, tenant, p2) == ([], 0)
    assert len(told(db, tenant, default, consume=False)[0]) == 1


def test_the_end_users_the_key_may_read_now(db, tenant, default):
    u1, u2 = fact(db, tenant.id, default.id), fact(db, tenant.id, default.id, user="u2")
    read(tenant.id, default.id, u1, u2)
    closed(db, u1)
    closed(db, u2)
    shown, more = told(db, tenant, default, EndUsers(["u2"]), consume=False)
    assert [c["fact_id"] for c in shown] == [fact_public_id(u2.id)] and more == 0, \
        "a change outside the key's end users is neither shown nor counted"
    assert [c["fact_id"] for c in told(db, tenant, default, "u1", consume=False)[0]] == \
        [fact_public_id(u1.id)]


def test_a_successor_the_key_may_not_read_is_no_longer_true(db, tenant, default):
    old = fact(db, tenant.id, default.id)
    read(tenant.id, default.id, old)
    closed(db, old, by=fact(db, tenant.id, default.id, user="u2", obj="Milan", agent="planner"))
    (c,), _ = told(db, tenant, default, EndUsers(["u1"]))
    assert c["now"] is None and c["by_agent"] is None and c["source_memory_id"] is None
    assert "Milan" not in str(c)


# ── the origin is the closure in force (review of piece 2, 8 Nov 2026) ─────
#
# `origin` was read from who wrote the fact closed and from any forget record
# of it: a forget that closed nothing relabelled a closure, a memory's past
# statement ending a caller's fact came out `stated`, and a forget of a fact
# whose end to come the judge had set came out `corrected`. It is the action
# that closed the fact, read where the closure is recorded, and an end the
# store records no author for is never told as explicit.

def _forget(client, head, f):
    return client.post(f"/v1/facts/{fact_public_id(f.id)}/forget", headers=head).json()


def test_a_forget_that_closed_nothing_relabels_nothing(client, db, tenant, default):
    head = _key(db, tenant)
    judged = fact(db, tenant.id, default.id)
    in_place = fact(db, tenant.id, default.id, subject="Luca", obj="Turin")
    read(tenant.id, default.id, judged, in_place)
    closed(db, judged, by=fact(db, tenant.id, default.id, obj="Milan"))
    closed(db, in_place)
    before = {c["fact_id"]: c["origin"] for c in told(db, tenant, default, consume=False)[0]}
    assert before == {fact_public_id(judged.id): changes.READ_FROM_MEMORY,
                      fact_public_id(in_place.id): changes.READ_FROM_MEMORY}, \
        "an end in place records no author: never told as stated"
    assert [_forget(client, head, f)["status"] for f in (judged, in_place)] == \
        ["already_forgotten"] * 2
    after = {c["fact_id"]: c["origin"] for c in told(db, tenant, default, consume=False)[0]}
    assert after == before, "a forget record that closed nothing is no closure"


def test_a_memorys_past_statement_ending_a_callers_fact_is_read_from_memory(
        db, tenant, default, monkeypatch):
    """The real path: a caller's fact (write_fact_direct), then a memory's
    past statement that ends it in place (`_persist_extracted`). The closed
    row is the caller's; the closure is the memory's."""
    from korely_agent.services.facts import contradiction, service
    from korely_agent.services.memory_core.fact_extractor import ExtractedFact

    def judge(candidates, *a, **k):
        return [c.id for c in candidates], {}

    monkeypatch.setattr(contradiction, "_classify", judge)
    monkeypatch.setattr(contradiction, "_classify_batch",
                        lambda groups, *a, **k: {g["new_id"]: judge(g["candidates"])[0]
                                                 for g in groups})
    service.write_fact_direct(db, tenant.id, subject="Maria", predicate="works_at", obj="Acme",
                              end_user_id="u1", project_id=default.id, subject_type="person")
    caller = db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id).one()
    assert caller.model_used == "direct"
    read(tenant.id, default.id, caller)
    mem = AgentMemory(tenant_id=tenant.id, project_id=default.id, end_user_id="u1",
                      content="Maria no longer works at Acme")
    db.add(mem)
    db.commit()
    service._persist_extracted(db, tenant.id, memory=mem, extracted=[ExtractedFact(
        subject="Maria", predicate_raw="works_at", object="Acme", confidence=0.9,
        subject_type="person", tense="past")])
    db.expire_all()
    assert db.get(AgentFact, caller.id).invalid_at is not None, "ended in place"
    (c,), _ = told(db, tenant, default, consume=False)
    assert c["origin"] == changes.READ_FROM_MEMORY and c["now"] is None


def test_a_forget_of_a_fact_whose_end_the_judge_had_set_ahead(client, db, tenant, default):
    """The judge closed the fact at its successor's start, ten days ahead;
    the caller forgets it today. Forgotten, not corrected, and nothing holds
    now in its place: the successor begins in ten days."""
    head = _key(db, tenant)
    later = fact(db, tenant.id, default.id, obj="Milan")
    later.valid_from = datetime.now(timezone.utc) + timedelta(days=10)
    db.commit()
    f = fact(db, tenant.id, default.id)
    read(tenant.id, default.id, f)
    closed(db, f, by=later, at=later.valid_from)
    assert told(db, tenant, default, consume=False) == ([], 0), "true until its end"
    assert _forget(client, head, f)["status"] == "forgotten"
    (c,), _ = told(db, tenant, default, consume=False)
    assert c["origin"] == changes.FORGOTTEN
    assert c["now"] is None and c["by_agent"] is None and c["source_memory_id"] is None


def test_a_forget_ahead_then_the_judge_closes_it_sooner(client, db, tenant, default):
    """A forget dated ahead, then the judge ends the fact before that date:
    the closure in force is the judge's, and the forget record is not it."""
    head = _key(db, tenant)
    f = fact(db, tenant.id, default.id)
    read(tenant.id, default.id, f)
    ahead = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    r = client.post(f"/v1/facts/{fact_public_id(f.id)}/forget", headers=head,
                    json={"at": ahead})
    assert r.status_code == 200 and r.json()["status"] == "forgotten", r.text
    by_caller = fact(db, tenant.id, default.id, obj="Milan", model="direct")
    closed(db, f, by=by_caller)
    (c,), _ = told(db, tenant, default, consume=False)
    assert c["origin"] == changes.STATED and c["now"]["fact_id"] == fact_public_id(by_caller.id)


def test_what_holds_now_is_what_is_true_now(db, tenant, default):
    """Down the chain, the fact that holds now has begun: a successor dated
    ahead is not what holds now."""
    f = fact(db, tenant.id, default.id)
    read(tenant.id, default.id, f)
    later = fact(db, tenant.id, default.id, obj="Milan")
    later.valid_from = datetime.now(timezone.utc) + timedelta(days=10)
    db.commit()
    closed(db, f, by=later, at=datetime.now(timezone.utc) - timedelta(seconds=1))
    (c,), _ = told(db, tenant, default, consume=False)
    assert c["now"] is None


# ── a forgotten memory's words are never told (review of piece 2) ─────────

def _memory(db, tenant, default, text):
    m = AgentMemory(tenant_id=tenant.id, project_id=default.id, end_user_id="u1",
                    content=text, graph_sync_status="done")
    db.add(m)
    db.commit()
    return m


def _of(db, f, *memories):
    f.source_memory_id = memories[0].id
    f.source_memory_ids = [str(m.id) for m in memories]
    f.observation_count = len(memories)
    db.commit()
    return f


def test_a_forgotten_memorys_words_are_not_told(db, tenant, default):
    from korely_agent.services.facts import erasure

    m1, m2 = _memory(db, tenant, default, "rome"), _memory(db, tenant, default, "milan")
    old = _of(db, fact(db, tenant.id, default.id, obj="Secret-Rome"), m1)
    new = _of(db, fact(db, tenant.id, default.id, obj="Milan"), m2)
    read(tenant.id, default.id, old)
    closed(db, old, by=new)
    assert "Secret-Rome" in told(db, tenant, default, consume=False)[0][0]["was"]
    erasure.forget_memory(db, tenant.id, m1.id, project_id=default.id)
    (c,), _ = told(db, tenant, default, consume=False)
    assert c["was"] is None and "Secret-Rome" not in str(c), c
    assert c["now"]["fact_id"] == fact_public_id(new.id), "the change is still told"
    erasure.forget_memory(db, tenant.id, m2.id, project_id=default.id)
    (c,), _ = told(db, tenant, default, consume=False)
    assert c["was"] is None and c["now"] is None


def test_an_end_in_place_whose_memory_is_forgotten_later_is_still_told(db, tenant, default):
    """The memory's forget did not close it (it had ended already): the
    change stays, without the memory's words. A fact its memory's forget
    closed is no change (test_a_memory_forgotten_gives_no_notice)."""
    from korely_agent.services.facts import erasure

    m3 = _memory(db, tenant, default, "luca")
    ended = _of(db, fact(db, tenant.id, default.id, subject="Luca", obj="Turin"), m3)
    read(tenant.id, default.id, ended)
    closed(db, ended)
    erasure.forget_memory(db, tenant.id, m3.id, project_id=default.id)
    (c,), _ = told(db, tenant, default, consume=False)
    assert c["fact_id"] == fact_public_id(ended.id) and c["was"] is None


def test_a_fact_another_live_memory_states_keeps_its_words(db, tenant, default):
    from korely_agent.services.facts import erasure

    m1, m2 = _memory(db, tenant, default, "rome"), _memory(db, tenant, default, "rome again")
    old = _of(db, fact(db, tenant.id, default.id), m1, m2)
    read(tenant.id, default.id, old)
    closed(db, old, by=fact(db, tenant.id, default.id, obj="Milan"))
    erasure.forget_memory(db, tenant.id, m1.id, project_id=default.id)
    (c,), _ = told(db, tenant, default, consume=False)
    assert c["was"].startswith("Maria lives in Rome")


# ── chains: what holds now, however long, and a loop ends (review of piece 2) ──

def _chain(db, tenant, default, n, *, last_open=True):
    links = [AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=default.id,
                       end_user_id="u1", subject_label="the counter", predicate_raw="is",
                       predicate_canonical="is", object_label=f"{k}", valid_from=START,
                       model_used="gemini-test", embedding=VECTOR,
                       embedding_status=vectors.DONE) for k in range(n)]
    db.add_all(links)
    db.flush()
    for x, y in zip(links, links[1:]):
        x.invalidated_by_id, x.invalid_at = y.id, datetime.now(timezone.utc)
    if not last_open:
        links[-1].invalid_at = datetime.now(timezone.utc)
    db.commit()
    return links


def test_a_fact_updated_sixty_times_has_its_value_now(db, tenant, default):
    """A bound of 50 links said `no longer true` of a fact with a value now."""
    links = _chain(db, tenant, default, 61)
    read(tenant.id, default.id, links[0])
    (c,), _ = told(db, tenant, default, consume=False)
    assert c["now"]["fact_id"] == fact_public_id(links[-1].id)


def test_a_correction_refused_on_history_names_the_value_sixty_links_down(db, tenant, default):
    """The 409 of a correction names the fact true now, down the same chain:
    the bound of 50 links named none."""
    from korely_agent.services.facts import service

    links = _chain(db, tenant, default, 61)
    with pytest.raises(service.FactNotCurrent) as refused:
        service.correct_fact(db, tenant.id, links[0].id, obj="x", project_id=default.id)
    assert refused.value.current_id == links[-1].id


def test_a_loop_in_the_chain_ends_with_nothing_now(db, tenant, default):
    a, b = fact(db, tenant.id, default.id, obj="A"), fact(db, tenant.id, default.id, obj="B")
    read(tenant.id, default.id, a)
    closed(db, a, by=b)
    closed(db, b, by=a)
    (c,), _ = told(db, tenant, default, consume=False)
    assert c["now"] is None


# ── a successor the key may not read tells nothing (review of piece 2) ─────

@pytest.mark.parametrize("model", ["direct", "gemini-test"])
@pytest.mark.parametrize("where", ["another end user", "another project"])
def test_nothing_of_a_successor_the_key_may_not_read(db, tenant, default, model, where):
    """Its text, its agent, its memory, and even its provenance through
    `origin`: a caller's and a memory's successor give the same answer."""
    mem = AgentMemory(tenant_id=tenant.id, project_id=default.id, end_user_id="u2",
                      agent_id="planner", content="Maria moved to Milan")
    db.add(mem)
    db.commit()
    if where == "another end user":
        project = default
        hidden = fact(db, tenant.id, default.id, user="u2", agent="planner", obj="Milan",
                      model=model, memory=mem)
    else:
        project = project_service.create_project(db, tenant_id=tenant.id, name="P2")
        hidden = fact(db, tenant.id, project.id, agent="planner", obj="Milan", model=model,
                      memory=mem)
    old = fact(db, tenant.id, default.id)
    read(tenant.id, default.id, old)
    closed(db, old, by=hidden)
    (c,), _ = told(db, tenant, default, EndUsers(["u1"]), consume=False)
    assert c["origin"] == changes.READ_FROM_MEMORY
    assert c["now"] is None and c["by_agent"] is None and c["source_memory_id"] is None
    assert "Milan" not in str(c)


def test_a_chain_through_a_fact_the_key_may_not_read_stops_there(db, tenant, default):
    f = fact(db, tenant.id, default.id)
    hidden = fact(db, tenant.id, default.id, user="u2", obj="Hidden")
    after = fact(db, tenant.id, default.id, obj="Naples")
    read(tenant.id, default.id, f)
    closed(db, hidden, by=after)
    closed(db, f, by=hidden)
    (c,), _ = told(db, tenant, default, EndUsers(["u1"]), consume=False)
    assert c["now"] is None, "what holds now is read only through what the key may read"


def test_a_chain_whose_later_link_the_key_may_not_read_stops_there(db, tenant, default):
    """The successor is the key's, the one after it is not: the walk stops
    there too (a mutant that walked every later link without the walls
    lived)."""
    f = fact(db, tenant.id, default.id)
    mine = fact(db, tenant.id, default.id, obj="Milan")
    hidden = fact(db, tenant.id, default.id, user="u2", obj="Hidden")
    after = fact(db, tenant.id, default.id, obj="Naples")
    read(tenant.id, default.id, f)
    closed(db, hidden, by=after)
    closed(db, mine, by=hidden)
    closed(db, f, by=mine)
    (c,), _ = told(db, tenant, default, EndUsers(["u1"]), consume=False)
    assert c["now"] is None and "Hidden" not in str(c) and "Naples" not in str(c)
    (c,), _ = told(db, tenant, default, consume=False)
    assert c["now"]["fact_id"] == fact_public_id(after.id), "the whole project's key sees it"


def test_a_chain_whose_later_link_is_in_another_project_stops_there(db, tenant, default):
    """The project wall on every link, not only the first (review of the
    corrections, 9 Nov 2026: the mutant P07b, a walk of the later links
    without the project wall, lived)."""
    p2 = project_service.create_project(db, tenant_id=tenant.id, name="P2")
    f = fact(db, tenant.id, default.id)
    mine = fact(db, tenant.id, default.id, obj="Milan")
    elsewhere = fact(db, tenant.id, p2.id, obj="Elsewhere")
    after = fact(db, tenant.id, default.id, obj="Naples")
    read(tenant.id, default.id, f)
    closed(db, elsewhere, by=after)
    closed(db, mine, by=elsewhere)
    closed(db, f, by=mine)
    (c,), _ = told(db, tenant, default, consume=False)
    assert c["now"] is None and "Elsewhere" not in str(c) and "Naples" not in str(c)


def test_a_fact_deleted_between_the_statements_leaves_the_others(db, tenant, default,
                                                                monkeypatch):
    """A fact deleted after the first query of the lookup is no change any
    more, and the others are still told (review of piece 3: the lookup
    failed on it and the block told none)."""
    from korely_agent.db import get_sessionmaker
    from sqlalchemy import text

    victim, other = fact(db, tenant.id, default.id), fact(db, tenant.id, default.id, obj="Paris",
                                                         subject="Luca")
    read(tenant.id, default.id, victim, other)
    closed(db, victim, by=fact(db, tenant.id, default.id, obj="Milan"))
    closed(db, other, by=fact(db, tenant.id, default.id, obj="Lyon", subject="Luca"))
    real = changes.holding_now

    def deleting(*a, **k):
        s = get_sessionmaker()()
        try:
            s.execute(text("DELETE FROM agent_facts WHERE id = :i"), {"i": victim.id})
            s.commit()
        finally:
            s.close()
        return real(*a, **k)

    monkeypatch.setattr(changes, "holding_now", deleting)
    shown, more = told(db, tenant, default, consume=False)
    assert [c["fact_id"] for c in shown] == [fact_public_id(other.id)] and more == 0


def test_a_call_returns_two_hundred_at_most(db, tenant, default):
    facts = [AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=default.id,
                       end_user_id="u1", subject_label=f"S{i}", predicate_raw="has",
                       predicate_canonical="has", object_label=f"O{i}", valid_from=START,
                       invalid_at=datetime.now(timezone.utc), embedding=VECTOR,
                       embedding_status=vectors.DONE) for i in range(changes.MAX_CHANGES + 1)]
    db.add_all(facts)
    db.commit()
    read_marks.mark(tenant.id, default.id, BOT, [f.id for f in facts])
    shown, more = told(db, tenant, default, consume=False, limit=10_000)
    assert len(shown) == 200 and more == 1

# ── told once, to two callers at once (review of piece 2) ──────────────────

def test_two_callers_at_once_share_the_changes(db, tenant, default):
    import threading

    from korely_agent.db import get_sessionmaker

    facts = [fact(db, tenant.id, default.id, subject=f"P{i}") for i in range(30)]
    read(tenant.id, default.id, *facts)
    for f in facts:
        f.invalid_at = datetime.now(timezone.utc)
    db.commit()
    got = []
    barrier = threading.Barrier(2)

    def ask():
        s = get_sessionmaker()()
        try:
            barrier.wait()
            got.append([c["fact_id"] for c in changes.changed_since(
                s, tenant.id, default.id, BOT, EVERY_END_USER, limit=30)[0]])
        finally:
            s.close()

    threads = [threading.Thread(target=ask) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not set(got[0]) & set(got[1]), "a change was told to both"
    assert len(got[0]) + len(got[1]) == 30
    assert told(db, tenant, default) == ([], 0)


def test_a_change_not_reserved_counts_no_fact_deleted_meanwhile(db, tenant, default,
                                                                 monkeypatch):
    """The reservation gave way: nothing told, every change still waits, but
    not one whose fact was deleted between the statements."""
    from korely_agent.db import get_sessionmaker
    from sqlalchemy import text

    victim, other = fact(db, tenant.id, default.id), fact(db, tenant.id, default.id, obj="Paris",
                                                         subject="Luca")
    read(tenant.id, default.id, victim, other)
    closed(db, victim)
    closed(db, other)
    real = changes.holding_now

    def deleting(*a, **k):
        s = get_sessionmaker()()
        try:
            s.execute(text("DELETE FROM agent_facts WHERE id = :i"), {"i": victim.id})
            s.commit()
        finally:
            s.close()
        return real(*a, **k)

    monkeypatch.setattr(changes, "holding_now", deleting)
    monkeypatch.setattr(read_marks, "claim", lambda *a, **k: None)
    assert told(db, tenant, default) == ([], 1)
