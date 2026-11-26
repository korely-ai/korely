"""Hostile cases for the give-back of a fact told to several agents
(services/facts/given_back.py): the exclusion of F's own triple in `_open_beside`
must hide nothing that stands, and must reach no other account, project or end
user. Odd names, two accounts at once, a copy closed by hand, a deletion
repeated, a second closer, the agent left out, no end user. After a deletion every agent
holds at most one open copy, and nobody else's facts move."""
import uuid

import pytest

from korely_agent.db.models import AgentFact, Tenant
from tests.deletion_world import Fact, orphans
from tests.test_deleting_a_memory_gives_back_what_it_closed import (
    NEW, OLD, _delete, _moment, _open, _overlapping, _paris, _paris_in)
from tests.test_give_back_across_agents_of_one_end_user import _one_open_per_agent, _open_by_agent

pytest_plugins = ["tests.deletion_world"]


def test_the_agent_left_out_and_a_named_one_both_get_their_copy_back(world, db):
    w = world
    new = _paris_in(w, [None, "a"])
    assert _delete(w, new, False)["facts_reopened"] == 2
    db.expire_all()
    opened = [f.agent_id for f in db.query(AgentFact) if f.invalid_at is None and f.object_label == "Paris"]
    assert len(opened) == 2 and len(set(opened)) == 2
    assert orphans(db) == []


@pytest.mark.parametrize("told", ["PARIS", "paris", "Paris "], ids=["upper", "lower", "trailing"])
def test_a_name_that_differs_only_in_case_or_space_is_the_same_value(world, db, told):
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads["I live in Paris, again."] = [Fact("u1", "lives_in", told)]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.write(OLD, agent="a")
    w.write("I live in Paris, again.", agent="b")
    w.stale = ("aris", "ARIS")
    new = w.write(NEW, agent="z")
    assert _open(db) == ["Zanzibar"]
    _delete(w, new, False)
    _one_open_per_agent(db, "Paris")
    db.expire_all()
    assert sorted(f.agent_id for f in db.query(AgentFact)
                  if f.invalid_at is None and f.object_label.strip().lower() == "paris") == ["a", "b"]
    assert orphans(db) == []


def test_the_same_words_in_another_account_are_not_touched_and_do_not_block(world, db):
    w = world
    new = _paris_in(w, ["a", "b"])
    t1 = w.tenant
    t2 = Tenant(id=uuid.uuid4(), name="Other Insurance")
    db.add(t2)
    db.commit()
    w.tenant = t2
    w.use()
    w.write(OLD, agent="a")
    w.write(OLD, agent="b")
    w.stale = ("Paris",)
    w.write(NEW, agent="z")
    w.stale = ()
    w.tenant = t1
    w.use()
    other_before = sorted(str(f.id) for f in db.query(AgentFact).filter(AgentFact.tenant_id == t2.id)
                          if f.invalid_at is None)
    assert _delete(w, new, False)["facts_reopened"] == 2
    db.expire_all()
    mine = [f for f in db.query(AgentFact) if f.tenant_id == t1.id and f.invalid_at is None
            and f.object_label == "Paris"]
    assert sorted(f.agent_id for f in mine) == ["a", "b"]
    after = sorted(str(f.id) for f in db.query(AgentFact).filter(AgentFact.tenant_id == t2.id)
                   if f.invalid_at is None)
    assert after == other_before
    assert orphans(db) == []


def test_an_open_copy_in_another_project_neither_blocks_nor_is_touched(world, db):
    from korely_agent.services import projects as project_service

    w = world
    new = _paris_in(w, ["a", "b"])
    beta = project_service.create_project(db, tenant_id=w.tenant.id, name="Beta")
    db.commit()
    w.use(project_id=beta.id)
    w.write(OLD, agent="a")
    w.use()
    db.expire_all()
    beta_open = [str(f.id) for f in db.query(AgentFact)
                 if f.project_id == beta.id and f.invalid_at is None]
    assert len(beta_open) == 1
    assert _delete(w, new, False)["facts_reopened"] == 2
    db.expire_all()
    assert [str(f.id) for f in db.query(AgentFact)
            if f.project_id == beta.id and f.invalid_at is None] == beta_open
    assert len([f for f in db.query(AgentFact) if f.project_id != beta.id and f.invalid_at is None
                and f.object_label == "Paris"]) == 2


def test_a_copy_closed_by_hand_in_one_agent_stays_closed(world, db):
    """b's copy is closed by hand (`forget` with its `at`) while it is open,
    before Zanzibar is told: it names no closer, so forgetting Zanzibar gives
    back a's copy only, and b's stays closed where the developer closed it."""
    from korely_agent.services.ids import fact_public_id

    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.write(OLD, agent="a")
    w.write(OLD, agent="b")
    db.expire_all()
    b = next(f for f in db.query(AgentFact) if f.agent_id == "b")
    at = _moment()
    r = w.client.post(f"/v1/facts/{fact_public_id(b.id)}/forget", json={"at": at.isoformat()})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "forgotten"
    w.stale = ("Paris",)
    new = w.write(NEW, agent="z")
    assert _open(db) == ["Zanzibar"]
    assert _delete(w, new, False)["facts_reopened"] == 1
    assert _open_by_agent(db, "Paris") == [("a", "u1")]
    (b,) = [f for f in _paris(db) if f.agent_id == "b"]
    assert b.invalid_at == at and b.invalidated_by_id is None
    for a in ("a", "b"):
        assert _overlapping([f for f in _paris(db) if f.agent_id == a]) == []
    assert orphans(db) == []


def test_the_count_matches_the_copies_really_open_after_each_deletion(world, db):
    w = world
    new = _paris_in(w, ["a", "b", "d"])
    before = len(_open_by_agent(db, "Paris"))
    out = _delete(w, new, True)
    assert len(_open_by_agent(db, "Paris")) - before == out["facts_reopened"] == 3
    for _ in range(3):
        assert w.client.delete(f"/v1/memories/{new}").status_code == 404
    assert len(_open_by_agent(db, "Paris")) == 3


@pytest.mark.parametrize("told", ["Rome", "Paris"], ids=["another-value", "f-object"])
def test_a_value_open_under_the_closers_relation_in_another_agent_blocks_all(world, db, told):
    """The closer is told under a relation of its own ("I left Paris", which
    the judge reads beside lives_in Paris: same subject, the same object),
    and a value stands under that relation in another agent: no copy of
    lives_in Paris comes back. `left Paris` there is not F's own triple (the
    relation differs), so it is not left out as one, and blocks too."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads["I left Paris."] = [Fact("u1", "left", "Paris")]
    for a in ("a", "b", "d"):
        w.write(OLD, agent=a)
    w.stale = ("Paris",)
    new = w.write("I left Paris.", agent="z")
    db.expire_all()
    assert sorted((f.agent_id, f.predicate_canonical) for f in db.query(AgentFact)
                  if f.invalid_at is None) == [("z", "left")]
    w.stale = ()
    w.reads[f"I left {told} too."] = [Fact("u1", "left", told)]
    w.write(f"I left {told} too.", agent="y")
    assert _delete(w, new, False)["facts_reopened"] == 0
    db.expire_all()
    assert sorted((f.agent_id, f.predicate_canonical, f.object_label) for f in db.query(AgentFact)
                  if f.invalid_at is None) == [("y", "left", told)]
    assert len([f for f in _paris(db) if f.predicate_canonical == "lives_in"]) == 3
    assert orphans(db) == []


@pytest.mark.parametrize("name", ["object_canonical", "subject_canonical"])
def test_a_value_standing_with_no_canonical_name_still_blocks(world, db, name):
    """A standing row whose canonical object or subject is NULL is read by
    its label (`given_back._held`): Rome is not F's own triple, and it is
    about F's subject. A plain `=` on the NULL, negated in `_its_triple` or
    on the subject in `_open_beside`, would drop the row and let both copies
    back beside it."""
    from sqlalchemy import text

    w = world
    new = _paris_in(w, ["a", "b"])
    w.stale = ()
    w.reads["Since last week I live in Rome."] = [Fact("u1", "lives_in", "Rome")]
    w.write("Since last week I live in Rome.", agent="y")
    db.execute(text(f"UPDATE agent_facts SET {name} = NULL WHERE object_label = 'Rome'"))
    db.commit()
    assert _delete(w, new, False)["facts_reopened"] == 0
    assert _open(db) == ["Rome"]
    assert orphans(db) == []


@pytest.mark.parametrize("order", [(0, 1), (1, 0)], ids=["zanzibar-first", "oslo-first"])
def test_two_closers_deleted_in_either_order_never_leave_two_open_for_one_agent(world, db, order):
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.write(OLD, agent="a")
    w.write(OLD, agent="b")
    w.stale = ("Paris",)
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.reads["Moved to Oslo."] = [Fact("u1", "lives_in", "Oslo")]
    ids = [w.write(NEW, agent="z"), w.write("Moved to Oslo.", agent="y")]
    w.stale = ()
    for i in order:
        _delete(w, ids[i], False)
        _one_open_per_agent(db)
        for a in ("a", "b"):
            assert _overlapping([f for f in _paris(db) if f.agent_id == a]) == []
        assert orphans(db) == []


def test_the_closer_told_in_the_same_agent_as_a_copy_gives_every_agent_its_copy_back(world, db):
    """z tells Zanzibar to an agent that also holds a Paris copy: forgetting
    it gives back a, b and z's own Paris, one open copy each."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.write(OLD, agent="a")
    w.write(OLD, agent="z")
    w.stale = ("Paris",)
    new = w.write(NEW, agent="z")
    w.stale = ()
    assert _delete(w, new, False)["facts_reopened"] == 2
    _one_open_per_agent(db)
    assert sorted(a for a, _ in _open_by_agent(db, "Paris")) == ["a", "z"]
    assert _open(db) == ["Paris", "Paris"]
    assert orphans(db) == []


def test_a_forget_then_an_erase_of_the_same_memory_gives_back_once(world, db):
    w = world
    new = _paris_in(w, ["a", "b"])
    assert _delete(w, new, False)["facts_reopened"] == 2
    assert _delete(w, new, True)["facts_reopened"] == 0
    _one_open_per_agent(db)
    assert len(_open_by_agent(db, "Paris")) == 2
    assert orphans(db) == []


def test_the_copies_come_back_for_an_end_user_of_odd_characters(world, db):
    """Two end users whose ids differ only in case or carry spaces and
    punctuation: each keeps its own copies, none blocks the other."""
    w = world
    users = ["U 1", "u 1", "ü.ü:1"]
    for u in users:
        w.reads[f"{u} lives in Paris"] = [Fact(u, "lives_in", "Paris")]
        w.reads[f"{u} now lives in Zanzibar"] = [Fact(u, "lives_in", "Zanzibar")]
    ids = {}
    for u in users:
        w.write(f"{u} lives in Paris", user=u, agent="a")
        w.write(f"{u} lives in Paris", user=u, agent="b")
    w.stale = ("Paris",)
    for u in users:
        ids[u] = w.write(f"{u} now lives in Zanzibar", user=u, agent="z")
    w.stale = ()
    db.expire_all()
    assert _open(db) == ['Zanzibar'] * 3, _open(db)
    _delete(w, ids[users[0]], False)
    opened = _open_by_agent(db, "Paris")
    assert len(opened) == 2 and len({e for _, e in opened}) == 1
    for u in users[1:]:
        _delete(w, ids[u], False)
    _one_open_per_agent(db)
    assert len(_open_by_agent(db, "Paris")) == 6
    assert orphans(db) == []


def _paris_with_no_end_user(w, agents, closer="z"):
    """`_paris_in` for memories with no `user_id`: the extraction names the
    end user by the handle "user" (`extractor`), and the facts have no
    end_user_id. Returns Zanzibar's memory id, written by `closer`: since
    memory_core 6.28.0 it closes only the copy of its own agent (with no end
    user the judge weighs a fact against its agent's facts only)."""
    w.reads["I live in Paris, no end user."] = [Fact("user", "lives_in", "Paris")]
    w.reads["Now I live in Zanzibar, no end user."] = [Fact("user", "lives_in", "Zanzibar")]
    w.stale = ()
    for a in agents:
        w.write("I live in Paris, no end user.", user=None, agent=a)
    w.stale = ("Paris",)
    return w.write("Now I live in Zanzibar, no end user.", user=None, agent=closer)


@pytest.mark.parametrize("erase", [False, True], ids=["forget", "erase"])
@pytest.mark.parametrize("closer", ["z", "a"], ids=["another-agent", "agent-a"])
def test_with_no_end_user_a_copy_comes_back_only_to_the_agent_that_closed_it(world, db, erase, closer):
    """Turned in memory_core 6.28.0. Until 6.27.0 no end user was one
    memory of every agent for the judge too, so agent z's Zanzibar closed
    the copies of a and b, and a deletion gave each one back. Now with no
    end user an agent closes only its own facts: z's Zanzibar closes no
    copy (nothing to give back), a's closes a's, and its deletion gives
    back that one, with no overlap."""
    w = world
    new = _paris_with_no_end_user(w, ["a", "b"], closer=closer)
    kept = ["b"] if closer == "a" else ["a", "b"]
    assert [a for a, _u in _rows_open_paris(db)] == kept
    assert _delete(w, new, erase)["facts_reopened"] == (1 if closer == "a" else 0)
    _one_open_per_agent(db)
    assert _open_by_agent(db, "Paris") == [("a", None), ("b", None)]
    for a in ("a", "b"):
        assert _overlapping([f for f in _paris(db) if f.agent_id == a]) == []
    assert orphans(db) == []


def test_no_end_user_and_an_end_user_do_not_reach_each_other(world, db):
    """The same agents and the same value with no end user and with u1, each
    closed by its own Zanzibar: deleting u1's gives u1 its copies and moves
    no row of no end user, and the other way round."""
    w = world
    mine = _paris_in(w, ["a", "b"])
    # memory_core 6.28.0: with no end user, Zanzibar closes its agent's copy only.
    nobody = _paris_with_no_end_user(w, ["a", "b"], closer="a")
    assert _open(db) == ["Paris", "Zanzibar", "Zanzibar"], "b's copy with no end user stays open"

    def rows_of(user):
        db.expire_all()
        return sorted((str(f.id), f.invalid_at) for f in db.query(AgentFact)
                      if f.end_user_id == user)

    def open_paris(user):
        return [a for a, u in _rows_open_paris(db) if u == user]

    before = rows_of(None)
    assert _delete(w, mine, False)["facts_reopened"] == 2
    assert rows_of(None) == before
    assert open_paris("u1") == ["a", "b"] and open_paris(None) == ["b"]
    before = rows_of("u1")
    assert _delete(w, nobody, False)["facts_reopened"] == 1
    assert rows_of("u1") == before
    assert open_paris("u1") == ["a", "b"] and open_paris(None) == ["a", "b"]
    assert orphans(db) == []


def _rows_open_paris(db):
    # `_open_by_agent` sorts (agent, end user) pairs, and a None beside a
    # string does not sort: by agent only here.
    db.expire_all()
    return sorted(((f.agent_id, f.end_user_id) for f in db.query(AgentFact)
                   if f.invalid_at is None and f.object_label == "Paris"), key=lambda r: r[0])


def test_a_copy_of_no_end_user_already_open_is_not_doubled(world, db, monkeypatch):
    """Agent b is told Paris again, with no end user, while Zanzibar
    stands: forgetting Zanzibar finds b's open row ((d), `_told_since`, the
    end user compared NULL equal to NULL) before a second one is built for
    the one-open-row index to refuse. Until memory_core 6.27.0 one Zanzibar
    of agent z had closed a's and b's copies, and the forget gave a its copy;
    since 6.28.0 an agent closes only its own facts with no end user, so
    each agent closed its copy with a Zanzibar of its own, and forgetting
    b's builds nothing and leaves a's closed by a's."""
    from korely_agent.services.facts import given_back

    w = world
    _paris_with_no_end_user(w, ["a"], closer="a")
    new = _paris_with_no_end_user(w, ["b"], closer="b")
    w.stale = ()
    w.write("I live in Paris, no end user.", user=None, agent="b")
    built = []
    real = given_back._continuation
    monkeypatch.setattr(given_back, "_continuation",
                        lambda f, *a: built.append(f.agent_id) or real(f, *a))
    assert _delete(w, new, False)["facts_reopened"] == 0
    assert built == []
    assert [a for a, u in _rows_open_paris(db) if u is None] == ["b"]
    assert orphans(db) == []


@pytest.mark.parametrize("erase", [False, True], ids=["forget", "erase"])
def test_with_no_end_user_another_agents_value_does_not_keep_a_fact_closed(world, db, erase):
    """The probe of the review of 26 Nov 2026 (memory_core 6.28.0): agent a
    writes Paris, agent b Rome, then a's Zanzibar closes a's Paris (with no
    end user the judge weighs a's facts only). Forgetting a's Zanzibar gives
    a its Paris back: b's Rome could not have closed it, and does not keep
    it closed. On db4bad67 (6.28.0 before this) Paris stayed closed and a
    had no value at all."""
    w = world
    w.reads["I live in Paris, no end user."] = [Fact("user", "lives_in", "Paris")]
    w.reads["I live in Rome, no end user."] = [Fact("user", "lives_in", "Rome")]
    w.reads["Now I live in Zanzibar, no end user."] = [Fact("user", "lives_in", "Zanzibar")]
    w.stale = ()
    w.write("I live in Paris, no end user.", user=None, agent="a")
    w.write("I live in Rome, no end user.", user=None, agent="b")
    w.stale = ("Paris",)
    new = w.write("Now I live in Zanzibar, no end user.", user=None, agent="a")
    assert [a for a, _u in _rows_open_paris(db)] == [], "a's Zanzibar closed a's Paris"
    assert _delete(w, new, erase)["facts_reopened"] == 1
    assert [a for a, _u in _rows_open_paris(db)] == ["a"]
    db.expire_all()
    assert sorted((f.agent_id, f.object_label) for f in db.query(AgentFact) if f.invalid_at is None) == [
        ("a", "Paris"), ("b", "Rome")]
    assert orphans(db) == []
