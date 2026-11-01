"""The same statement saved many times is one fact, and one correction closes
it everywhere.

Backs the claim against the copies problem described in the comments of mem0
issue #5867 (mem0ai/mem0): the same statement lives in several memories (or
was saved by several agents), the user corrects it once, and a stale copy
stays open as if it were current.

What Korely does, and what these tests hold it to:

- restating a statement reconfirms ONE fact (a count and a list of sources,
  `observation_count`, `source_memory_ids`), it never makes a second open row;
- an agent's namespace keeps its own row (`agent_id`), but a correction
  reaches every namespace of the end user: written by anyone, or made
  with PATCH /v1/facts/{id};
- forgetting the copies one memory at a time keeps the fact while one memory
  still states it, and closes it with the last;
- editing one of the memories to a new value is judged against the copies
  the other memories still state, as a new memory saying it would be
  (memory_core 6.2.0, `store.shared_by_the_previous_version`), never
  against what the same new text states again, in any order.

The writer is scripted (tests/deletion_world.py): it reads each text into the
fact the test says, and the judge closes what the test calls stale. Which
facts the judge is shown is the product's own doing, and is what is tested.
"""
import threading
import time

import pytest
from sqlalchemy.orm import sessionmaker

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.ids import fact_public_id, parse_mem_id
from tests.deletion_world import Fact

# the `world` fixture
pytest_plugins = ["tests.deletion_world"]

ACME = Fact("u1", "works_at", "Acme")
COPIES = ["I work at Acme.", "Acme is my employer.", "My job is at Acme these days."]
BETA = "I changed jobs: now I work at Beta."


def facts(db, obj=None, *, open_only=False):
    db.expire_all()
    q = db.query(AgentFact)
    return [f for f in q.order_by(AgentFact.created_at)
            if (obj is None or f.object_label == obj) and (not open_only or f.invalid_at is None)]


def raw(public_ids):
    """The memories' ids as a fact's sources hold them (the UUID's text)."""
    return sorted(str(parse_mem_id(i)) for i in public_ids)


def saved_everywhere(w, agents=("a1", "a2", "a3")):
    """Three memories in the end user's own namespace, one by each of three agents."""
    for t in COPIES:
        w.reads[t] = [ACME]
        w.write(t)
    w.reads["I work at Acme!"] = [ACME]
    for a in agents:
        w.write("I work at Acme!", agent=a)


def test_restating_a_statement_reconfirms_one_fact(world, db):
    """mem0 #5867 (copies): three memories say it in three ways, and there is
    one fact, three times confirmed, with three sources; never three rows
    that a later correction would have to find one by one."""
    w = world
    for t in COPIES:
        w.reads[t] = [ACME]
        w.write(t)
    [f] = facts(db, "Acme")
    assert f.invalid_at is None
    assert f.observation_count == 3
    assert len(f.source_memory_ids) == 3 and len(set(f.source_memory_ids)) == 3
    assert len(facts(db)) == 1


@pytest.mark.parametrize("corrector", [None, "a1", "a9"], ids=["no agent", "an agent with a copy",
                                                               "an agent with none"])
def test_one_correction_written_by_anyone_closes_every_copy(world, db, corrector):
    """mem0 #5867: the user says it once more, changed, from whichever
    namespace; the copies of the three agents and of the end user's own go
    with it, and no read shows the old value as current."""
    w = world
    saved_everywhere(w)
    assert len(facts(db, "Acme", open_only=True)) == 4  # one per namespace
    w.stale = ("Acme",)
    w.reads[BETA] = [Fact("u1", "works_at", "Beta")]
    w.write(BETA, agent=corrector)
    assert facts(db, "Acme", open_only=True) == [], "a stale copy stays open as current"
    assert len(facts(db, "Beta", open_only=True)) == 1
    # What the customer reads: the facts, and the context an agent is given.
    body = w.client.get("/v1/facts", params={"user_id": "u1"}).text
    assert "Acme" not in body
    context = w.client.get("/v1/context", params={"user_id": "u1", "query": "job"}).json()["context"]
    known = context.split("## Known facts", 1)[1].split("##", 1)[0]
    assert "Acme" not in known and "Beta" in known
    # The memories keep their words, and say that what they state changed.
    for line in context.splitlines():
        if line.startswith("- [") and "Acme" in line:
            assert "changed later" in line, f"a stale memory is shown as true: {line}"


def test_one_correction_by_the_api_closes_every_copy(world, db):
    """mem0 #5867: PATCH /v1/facts/{id} on ONE copy (the end user's own); the
    copies the three agents kept are judged against the corrected value and
    go too."""
    w = world
    saved_everywhere(w)
    w.stale = ("Acme",)
    mine = next(f for f in facts(db, "Acme") if f.agent_id is None)
    r = w.client.patch(f"/v1/facts/{fact_public_id(mine.id)}", json={"object": "Beta"})
    assert r.status_code == 200, r.text
    assert facts(db, "Acme", open_only=True) == [], "a stale copy stays open as current"
    assert [f.object_label for f in facts(db, open_only=True)] == ["Beta"]


def test_forgetting_the_fact_forgets_all_its_memories_statements(world, db):
    """mem0 #5867: POST /v1/facts/{id}/forget on the fact the three memories
    state closes the one row they share; there is no second copy to forget."""
    w = world
    for t in COPIES:
        w.reads[t] = [ACME]
        w.write(t)
    [f] = facts(db, "Acme")
    r = w.client.post(f"/v1/facts/{fact_public_id(f.id)}/forget")
    assert r.status_code == 200, r.text
    assert facts(db, "Acme", open_only=True) == []
    assert "Acme" not in w.client.get("/v1/facts", params={"user_id": "u1"}).text


def test_forgetting_the_copies_one_by_one_keeps_the_fact_until_the_last(world, db):
    """mem0 #5867 seen from the other side: a fact three memories state is
    not closed by forgetting one, and no copy survives the last."""
    w = world
    ids = []
    for t in COPIES:
        w.reads[t] = [ACME]
        ids.append(w.write(t))
    for n, mid in enumerate(ids):
        assert w.client.delete(f"/v1/memories/{mid}").status_code == 200
        left = facts(db, "Acme", open_only=True)
        if n < 2:
            [f] = left
            assert f.observation_count == 3 - (n + 1), "the count still counts a forgotten memory"
            assert len(f.source_memory_ids) == 3 - (n + 1)
        else:
            assert left == [], "the last copy was forgotten and the fact is still open"


def test_a_copy_edited_to_the_new_value_closes_the_others(world, db):
    """mem0 #5867: the user edits ONE of three memories to the new value; the
    other two still say Acme in their text, but the fact is closed and the
    context says so, instead of showing Acme as current."""
    w = world
    ids = []
    for t in COPIES:
        w.reads[t] = [ACME]
        ids.append(w.write(t))
    w.stale = ("Acme",)
    w.reads[BETA] = [Fact("u1", "works_at", "Beta")]
    r = w.client.patch(f"/v1/memories/{ids[0]}", json={"content": BETA})
    assert r.status_code == 200, r.text
    assert facts(db, "Acme", open_only=True) == []
    assert [f.object_label for f in facts(db, open_only=True)] == ["Beta"]
    [beta] = facts(db, "Beta", open_only=True)
    [acme] = facts(db, "Acme")
    # Closed by the judge, pointing at the new value, from where it began
    # (Graphiti section 2.2.3), and stated by the two memories that still say it.
    assert acme.invalidated_by_id == beta.id
    assert acme.invalid_at == beta.valid_from
    assert sorted(acme.source_memory_ids) == raw(ids[1:])
    assert acme.observation_count == 2 and str(acme.source_memory_id) in raw(ids[1:])
    assert beta.source_memory_ids == raw(ids[:1])
    context = w.client.get("/v1/context", params={"user_id": "u1", "query": "job"}).json()["context"]
    known = context.split("## Known facts", 1)[1].split("##", 1)[0]
    assert "Acme" not in known and "Beta" in known


@pytest.mark.parametrize("edited", ["own copy", "an agent's copy"])
def test_an_edit_closes_the_copies_of_every_namespace(world, db, edited):
    """mem0 #5867 across agents: the end user's three memories share one row,
    and three agents keep a row each; one memory edited to the new value,
    from the shared row or from an agent's own, leaves no copy open."""
    w = world
    ids = []
    for t in COPIES:
        w.reads[t] = [ACME]
        ids.append(w.write(t))
    w.reads["I work at Acme!"] = [ACME]
    agents = [w.write("I work at Acme!", agent=a) for a in ("a1", "a2", "a3")]
    assert len(facts(db, "Acme", open_only=True)) == 4
    w.stale = ("Acme",)
    w.reads[BETA] = [Fact("u1", "works_at", "Beta")]
    target = ids[0] if edited == "own copy" else agents[0]
    r = w.client.patch(f"/v1/memories/{target}", json={"content": BETA})
    assert r.status_code == 200, r.text
    assert facts(db, "Acme", open_only=True) == [], "a stale copy stays open as current"
    assert [f.object_label for f in facts(db, open_only=True)] == ["Beta"]
    assert "Acme" not in w.client.get("/v1/facts", params={"user_id": "u1"}).text


def test_two_copies_edited_in_turn(world, db):
    """The first edit closes the copies; the second, to the same new value,
    restates it: one Beta stated by both edited memories, one Acme closed
    once, still stated by the memory nobody edited."""
    w = world
    ids = []
    for t in COPIES:
        w.reads[t] = [ACME]
        ids.append(w.write(t))
    w.stale = ("Acme",)
    w.reads[BETA] = [Fact("u1", "works_at", "Beta")]
    assert w.client.patch(f"/v1/memories/{ids[0]}", json={"content": BETA}).status_code == 200
    again = "Beta is my employer now."
    w.reads[again] = [Fact("u1", "works_at", "Beta")]
    assert w.client.patch(f"/v1/memories/{ids[1]}", json={"content": again}).status_code == 200
    [beta] = facts(db, "Beta", open_only=True)
    assert sorted(beta.source_memory_ids) == raw(ids[:2]) and beta.observation_count == 2
    [acme] = facts(db, "Acme")
    assert acme.invalid_at is not None and acme.invalidated_by_id == beta.id
    # History: the closed row keeps the memory edited after it closed (it
    # did state Acme while Acme was true); the open rows are all Beta.
    assert raw(ids[2:])[0] in acme.source_memory_ids
    assert len(facts(db)) == 2
    # Then the second copy to a third value: Beta closes, Acme stays closed.
    gamma = "Now I work at Gamma."
    w.stale = ("Beta",)
    w.reads[gamma] = [Fact("u1", "works_at", "Gamma")]
    assert w.client.patch(f"/v1/memories/{ids[1]}", json={"content": gamma}).status_code == 200
    assert [f.object_label for f in facts(db, open_only=True)] == ["Gamma"]


def test_forgetting_the_edit_gives_the_copies_back(world, db):
    """The edit is forgotten: Beta goes with its only memory, and Acme, which
    the two other memories still say, is true again (facts/given_back.py),
    as a new row stated by those two."""
    w = world
    ids = []
    for t in COPIES:
        w.reads[t] = [ACME]
        ids.append(w.write(t))
    w.stale = ("Acme",)
    w.reads[BETA] = [Fact("u1", "works_at", "Beta")]
    assert w.client.patch(f"/v1/memories/{ids[0]}", json={"content": BETA}).status_code == 200
    r = w.client.delete(f"/v1/memories/{ids[0]}")
    assert r.status_code == 200, r.text
    assert r.json().get("facts_reopened") == 1
    [acme] = facts(db, "Acme", open_only=True)
    assert sorted(acme.source_memory_ids) == raw(ids[1:])
    assert facts(db, "Beta", open_only=True) == []


def test_an_edit_that_says_something_else_keeps_the_copies(world, db):
    """The edited memory now says something unrelated, and the judge closes
    nothing: the copies the two other memories state stay open, with those
    two sources; the edit only takes its own away."""
    w = world
    ids = []
    for t in COPIES:
        w.reads[t] = [ACME]
        ids.append(w.write(t))
    tea = "I like green tea."
    w.reads[tea] = [Fact("u1", "likes", "green tea")]
    assert w.client.patch(f"/v1/memories/{ids[0]}", json={"content": tea}).status_code == 200
    [acme] = facts(db, "Acme", open_only=True)
    assert sorted(acme.source_memory_ids) == raw(ids[1:]) and acme.observation_count == 2
    assert sorted(f.object_label for f in facts(db, open_only=True)) == ["Acme", "green tea"]


def test_an_edit_that_still_says_the_old_value_changes_nothing(world, db):
    """The edited memory says Acme in other words: the copy is the same row,
    with the same sources in the same order, the same first source, the same
    count, and the judge, which would close any Acme it is shown, is shown
    none (a memory's own facts never close each other, BUG-123)."""
    w = world
    ids = []
    for t in COPIES:
        w.reads[t] = [ACME]
        ids.append(w.write(t))
    [before] = facts(db, "Acme")
    sources, first = list(before.source_memory_ids), before.source_memory_id
    w.stale = ("Acme",)
    still = "Still at Acme, nothing changed."
    w.reads[still] = [ACME]
    for mid in (ids[0], ids[1]):
        assert w.client.patch(f"/v1/memories/{mid}", json={"content": still}).status_code == 200
    [after] = facts(db, "Acme")
    assert after.id == before.id and after.invalid_at is None
    assert after.source_memory_ids == sources and after.source_memory_id == first
    assert after.observation_count == 3


def test_an_edit_written_twice_is_written_once(world, db):
    """Idempotence: the same edit sent again (the same text, extracted
    again) adds no row and closes nothing more."""
    w = world
    ids = []
    for t in COPIES:
        w.reads[t] = [ACME]
        ids.append(w.write(t))
    w.stale = ("Acme",)
    w.reads[BETA] = [Fact("u1", "works_at", "Beta")]
    assert w.client.patch(f"/v1/memories/{ids[0]}", json={"content": BETA}).status_code == 200
    shape = [(f.id, f.object_label, f.invalid_at, list(f.source_memory_ids or []), f.observation_count)
             for f in facts(db)]
    assert w.client.patch(f"/v1/memories/{ids[0]}", json={"content": BETA}).status_code == 200
    assert [(f.id, f.object_label, f.invalid_at, list(f.source_memory_ids or []), f.observation_count)
            for f in facts(db)] == shape


@pytest.mark.parametrize("order", ["Acme first", "Beta first"])
@pytest.mark.parametrize("batched", [True, False], ids=["batched judge", "per-fact judge"])
def test_an_edit_that_states_the_old_value_beside_a_new_one_keeps_it(world, db, monkeypatch,
                                                                    order, batched):
    """The edited text says Acme AND Beta: Acme is the new version's own
    statement again, and a memory's statements never close each other
    (BUG-123), so the copy is not shown to the judge as Beta's candidate and
    stays the same open row, with its sources as they were: whichever comes
    first in the text, with the judgement of the whole write or with one
    judgement per fact, made before the text's later statements are written."""
    w = world
    ids = []
    for t in COPIES:
        w.reads[t] = [ACME]
        ids.append(w.write(t))
    [before] = facts(db, "Acme")
    sources, first = list(before.source_memory_ids), before.source_memory_id
    w.stale = ("Acme",)
    both = "I work at Acme and, on weekends, at Beta."
    beta = Fact("u1", "works_at", "Beta")
    w.reads[both] = [ACME, beta] if order == "Acme first" else [beta, ACME]
    from korely_agent.services.facts import service as S

    monkeypatch.setattr(S, "_BATCHED_JUDGE", batched)
    assert w.client.patch(f"/v1/memories/{ids[0]}", json={"content": both}).status_code == 200
    [after] = facts(db, "Acme")
    assert after.id == before.id and after.invalid_at is None
    assert after.source_memory_ids == sources and after.source_memory_id == first
    assert sorted(f.object_label for f in facts(db, open_only=True)) == ["Acme", "Beta"]


def test_a_past_statement_of_the_edit_never_closes_what_it_states_again(world, db):
    """The new text says "no longer likes Acme" (a past statement, judged as
    it is written, before the rest of the text) and "still works at Acme":
    the past statement's candidates are the facts about the same two
    entities, and the copy of works_at Acme is the text's own statement, so
    it is never among them. The copy stays the same row with its three
    sources; no second works_at Acme stated by the edited memory alone."""
    w = world
    ids = []
    for t in COPIES:
        w.reads[t] = [ACME]
        ids.append(w.write(t))
    [before] = facts(db, "Acme")
    sources, first = list(before.source_memory_ids), before.source_memory_id
    w.stale = ("Acme",)
    text = "I no longer like Acme, but I still work at Acme."
    w.reads[text] = [Fact("u1", "likes", "Acme", tense="past"), ACME]
    assert w.client.patch(f"/v1/memories/{ids[0]}", json={"content": text}).status_code == 200
    works = [f for f in facts(db, "Acme") if f.predicate_canonical == before.predicate_canonical]
    assert [f.id for f in works] == [before.id], "a second works_at Acme row"
    [after] = works
    assert after.invalid_at is None
    assert after.source_memory_ids == sources and after.source_memory_id == first


@pytest.fixture
def other(engine):
    """A second session, as another request or a worker has."""
    s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield s
    s.rollback()
    s.close()


def _during_the_judge(w, work, out):
    """Run `work` in another thread while the edit's judge is being asked,
    and wait for it at most 3 s there: what it found locked waits for the
    edit, and shows as still running when the judge answers."""
    def run():
        t0 = time.time()
        try:
            work()
        except Exception as e:  # noqa: BLE001 - the test reads it
            out["error"] = type(e).__name__
        out["seconds"] = time.time() - t0

    def hook():
        th = threading.Thread(target=run)
        th.start()
        th.join(timeout=3)
        out["waited"] = th.is_alive()
        out["thread"] = th

    w.hooks["judge"] = hook


def test_a_copy_forgotten_while_the_edit_is_judged_does_not_wait(world, db, other, tenant):
    """Nothing of the copy is written before the judge answers: a forget of
    another memory that states it, from another session while the model is
    asked, goes through at once, and the edit then takes its own source
    away from what that forget left."""
    from korely_agent.services import facts as agent_facts

    w = world
    ids = []
    for t in COPIES:
        w.reads[t] = [ACME]
        ids.append(w.write(t))
    pid = db.query(AgentMemory.project_id).filter(AgentMemory.tenant_id == tenant.id).first()[0]
    w.stale = ("Acme",)
    w.reads[BETA] = [Fact("u1", "works_at", "Beta")]
    out = {}
    _during_the_judge(w, lambda: agent_facts.forget_memory(other, tenant.id, parse_mem_id(ids[1]),
                                                           project_id=pid), out)
    assert w.client.patch(f"/v1/memories/{ids[0]}", json={"content": BETA}).status_code == 200
    out["thread"].join(timeout=30)
    assert "error" not in out, out
    assert not out["waited"] and out["seconds"] < 2, "the forget waited for the edit's judge"
    [acme] = facts(db, "Acme")
    assert acme.invalid_at is not None
    assert acme.source_memory_ids == raw(ids[2:]) and acme.observation_count == 1
    assert [f.object_label for f in facts(db, open_only=True)] == ["Beta"]


def test_two_copies_edited_at_once_both_leave_the_copy(world, db, other, tenant):
    """Two memories that state the copy edited at the same time, one while
    the other's judge is asked: each takes only its own source away, from
    the sources as the other left them, so neither comes back on the row."""
    from fastapi.testclient import TestClient

    from korely_agent.db import get_db
    from korely_agent.main import create_app

    w = world
    ids = []
    for t in COPIES:
        w.reads[t] = [ACME]
        ids.append(w.write(t))
    facts(db)  # the rows, loaded in the edit's session before it begins
    w.stale = ("Acme",)
    w.reads[BETA] = [Fact("u1", "works_at", "Beta")]
    note = "Nothing about work here."
    w.reads[note] = []
    app = create_app()
    app.dependency_overrides[get_db] = lambda: other
    out = {}
    with TestClient(app) as second:
        second.headers["Authorization"] = w.client.headers["Authorization"]
        _during_the_judge(w, lambda: out.setdefault(
            "status", second.patch(f"/v1/memories/{ids[1]}", json={"content": note}).status_code), out)
        assert w.client.patch(f"/v1/memories/{ids[0]}", json={"content": BETA}).status_code == 200
        out["thread"].join(timeout=30)
    assert "error" not in out and out.get("status") == 200, out
    assert not out["waited"], "the second edit waited for the first one's judge"
    [acme] = facts(db, "Acme")
    db.refresh(acme)
    assert acme.invalid_at is not None
    assert acme.source_memory_ids == raw(ids[2:]) and acme.observation_count == 1
