"""A fact told to several agents of one end user is a row in each agent's
namespace, and one memory can close them all (services/facts/given_back.py).
Deleting that memory gives each agent's copy back: three, five and ten agents
in any order, agents whose names differ only by case or script, forget and
erase, a repeated deletion, a copy whose memory is gone, an agent erased
first, a second end user with the same agents and words, a copy told again
in one agent (in the same letters or others), a write that lands in the
middle of the give-back, a value standing beside. After one deletion every
agent whose copy it closed has one open copy, never two, the closed rows are
as they were, and nobody else's facts move."""
import pytest

from korely_agent.db.models import AgentFact
from tests.deletion_world import Fact, orphans
from tests.test_deleting_a_memory_gives_back_what_it_closed import (
    NEW, OLD, ROME, _delete, _facts, _open, _overlapping, _paris, _paris_in)

pytest_plugins = ["tests.deletion_world"]


def _open_by_agent(db, label):
    db.expire_all()
    return sorted((f.agent_id, f.end_user_id) for f in db.query(AgentFact)
                  if f.invalid_at is None and f.object_label == label)


def _one_open_per_agent(db, label="Paris"):
    seen = {}
    for a, u in _open_by_agent(db, label):
        seen[(a, u)] = seen.get((a, u), 0) + 1
    assert all(n == 1 for n in seen.values()), seen


@pytest.mark.parametrize("agents", [["a", "b", "d"], ["d", "b", "a"], ["b", "a", "d", "e", "f"]],
                         ids=["abd", "dba", "five"])
@pytest.mark.parametrize("erase", [False, True], ids=["forget", "erase"])
def test_every_agent_of_the_end_user_gets_one_copy_back_in_any_order(world, db, agents, erase):
    w = world
    new = _paris_in(w, agents)
    assert _delete(w, new, erase)["facts_reopened"] == len(agents)
    assert sorted(a for a, _u in _open_by_agent(db, "Paris")) == sorted(agents)
    for a in agents:
        assert _overlapping([f for f in _paris(db) if f.agent_id == a]) == []
        assert len([f for f in _paris(db) if f.agent_id == a]) == 2
    assert orphans(db) == []


@pytest.mark.parametrize("name", ["object_canonical", "subject_canonical"])
@pytest.mark.parametrize("erase", [False, True], ids=["forget", "erase"])
def test_copies_with_no_canonical_name_come_back_to_every_agent(world, db, name, erase):
    """The column allows NULL. The copy given back to a holds its label
    there (`given_back._held`); read as the index reads it, NULL equal to
    NULL, it was another value for b's copy, which stayed closed."""
    from sqlalchemy import text

    w = world
    new = _paris_in(w, ["a", "b"])
    db.execute(text(f"UPDATE agent_facts SET {name} = NULL WHERE object_label = 'Paris'"))
    db.commit()
    assert _delete(w, new, erase)["facts_reopened"] == 2
    assert _open_by_agent(db, "Paris") == [("a", "u1"), ("b", "u1")]
    for a in ("a", "b"):
        assert _overlapping([f for f in _paris(db) if f.agent_id == a]) == []
    assert orphans(db) == []


def test_a_second_and_a_third_deletion_give_nothing_more(world, db):
    w = world
    new = _paris_in(w, ["a", "b"])
    assert _delete(w, new, False)["facts_reopened"] == 2
    assert w.client.delete(f"/v1/memories/{new}").status_code == 404
    assert _delete(w, new, True)["facts_reopened"] == 0
    assert _open(db) == ["Paris", "Paris"] and len(_paris(db)) == 4


def test_another_value_open_in_one_agent_keeps_every_copy_closed(world, db):
    """(c) still holds for a value that is not F's: Rome open beside keeps
    both copies closed, the same-triple exclusion does not hide it."""
    w = world
    new = _paris_in(w, ["a", "b"])
    w.stale = ()
    w.reads[ROME] = [Fact("u1", "lives_in", "Rome")]
    w.write(ROME, agent="b")
    out = _delete(w, new, False)
    assert out["facts_reopened"] == 0
    assert _open(db) == ["Rome"] and len(_paris(db)) == 2


def test_a_copy_whose_memory_is_gone_is_not_given_back_the_others_are(world, db):
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.reads["Paris, as told by b."] = [Fact("u1", "lives_in", "Paris")]
    w.write(OLD, agent="a")
    mb = w.write("Paris, as told by b.", agent="b")
    w.stale = ("Paris",)
    new = w.write(NEW, agent="z")
    assert _open(db) == ["Zanzibar"]
    _delete(w, mb, False)
    out = _delete(w, new, False)
    assert out["facts_reopened"] == 1
    assert _open_by_agent(db, "Paris") == [("a", "u1")]
    assert orphans(db) == []


def test_another_end_user_with_the_same_agents_and_words_is_not_touched(world, db):
    w = world
    new = _paris_in(w, ["a", "b"])
    w.reads["u2 lives in Paris."] = [Fact("u2", "lives_in", "Paris")]
    w.reads["u2 now lives in Zanzibar."] = [Fact("u2", "lives_in", "Zanzibar")]
    w.write("u2 lives in Paris.", user="u2", agent="a")
    w.write("u2 lives in Paris.", user="u2", agent="b")
    w.stale = ("Paris",)
    w.write("u2 now lives in Zanzibar.", user="u2", agent="z")
    before = _open_by_agent(db, "Zanzibar")
    assert _delete(w, new, False)["facts_reopened"] == 2
    assert _open_by_agent(db, "Paris") == [("a", "u1"), ("b", "u1")]
    assert [x for x in before if x[1] == "u2"] == _open_by_agent(db, "Zanzibar")


def test_a_copy_already_open_in_an_agent_is_not_doubled(world, db, monkeypatch):
    """Agent b told Paris again while Zanzibar stood: forgetting Zanzibar
    gives Paris back to a, and b's closed copy is not given back on top of
    its open one: (d) finds b's open row, before a row is built for the
    one-open-row index to refuse. One open Paris per agent."""
    from korely_agent.services.facts import given_back

    w = world
    new = _paris_in(w, ["a", "b"])
    w.stale = ()
    w.reads["Paris again."] = [Fact("u1", "lives_in", "Paris")]
    w.write("Paris again.", agent="b")
    built = []
    real = given_back._continuation
    monkeypatch.setattr(given_back, "_continuation",
                        lambda f, *a: built.append(f.agent_id) or real(f, *a))
    assert _delete(w, new, False)["facts_reopened"] == 1
    assert built == ["a"]
    assert _open_by_agent(db, "Paris") == [("a", "u1"), ("b", "u1")]
    for a in ("a", "b"):
        assert _overlapping([f for f in _paris(db) if f.agent_id == a]) == []
    assert orphans(db) == []


def test_a_second_closer_of_another_agent_keeps_its_value_standing(world, db):
    """Zanzibar closes a's Paris, then Oslo (agent y) closes b's Paris. Deleting
    Zanzibar gives nothing back: Oslo is open in the same relation (c), and
    the exclusion of F's own triple does not hide it. Oslo stays open."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.write(OLD, agent="a")
    w.stale = ("Paris",)
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    z1 = w.write(NEW, agent="z")
    w.stale = ()
    w.write(OLD, agent="b")
    w.stale = ("Paris",)
    w.reads["Moved to Oslo."] = [Fact("u1", "lives_in", "Oslo")]
    w.write("Moved to Oslo.", agent="y")
    out = _delete(w, z1, False)
    assert out["facts_reopened"] == 0
    assert _open(db) == ["Oslo"]
    assert orphans(db) == []


@pytest.mark.parametrize("first, second_began, back", [
    ("a", None, ("a", "Paris")), ("b", None, ("b", "Parigi")),
    ("a", "2019-01-01T00:00:00Z", ("b", "Parigi"))],
    ids=["a-older", "b-older", "b-told-later-began-earlier"])
def test_one_place_under_two_names_in_two_agents_is_two_values(world, db, first, second_began,
                                                               back):
    """The declared limit (docs/guide/limits.md): a name is resolved in its
    own agent's memory, so b's alias makes b's Paris "Parigi". The
    deletion gives back the copy that began earlier (`give_back` takes the
    closed rows by `valid_from`), told first or told later with an earlier
    `timestamp`, and that open copy is another value for the other one,
    which stays closed: no model is asked on a deletion."""
    from korely_agent.services import projects as project_service
    from korely_agent.services.facts.entities import declare_alias

    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    project = project_service.get_or_create_default_project(db, w.tenant.id)
    declare_alias(db, w.tenant.id, project.id, "u1", "b", "Paris", "Parigi")
    db.commit()
    w.write(OLD, agent=first)
    w.write(OLD, agent="b" if first == "a" else "a",
            **({"timestamp": second_began} if second_began else {}))
    assert {f.project_id for f in db.query(AgentFact)} == {project.id}
    w.stale = ("Paris",)
    new = w.write(NEW, agent="z")
    assert _open(db) == ["Zanzibar"]
    assert _delete(w, new, False)["facts_reopened"] == 1
    db.expire_all()
    assert sorted((f.agent_id, f.object_canonical) for f in db.query(AgentFact)
                  if f.invalid_at is None and f.agent_id in ("a", "b")) == [back]
    assert orphans(db) == []


def test_agents_whose_names_differ_only_by_case_or_script_are_different_agents(world, db):
    w = world
    agents = ["a", "A", "ü", "агент", "日本", "a b"]
    new = _paris_in(w, agents)
    assert _delete(w, new, False)["facts_reopened"] == len(agents)
    _one_open_per_agent(db)
    assert sorted(a for a, _ in _open_by_agent(db, "Paris")) == sorted(agents)
    for a in agents:
        assert _overlapping([f for f in _paris(db) if f.agent_id == a]) == []
    assert orphans(db) == []


def test_ten_agents_each_get_one_copy_back_and_the_count_says_so(world, db):
    """Ten is how many candidates the contradiction judge reads
    (`memory_core.candidates.SEMANTIC_LIMIT`): the most copies one closer
    closes at a write."""
    w = world
    agents = [f"agent-{i:02d}" for i in range(10)]
    new = _paris_in(w, agents)
    out = _delete(w, new, True)
    assert out["facts_reopened"] == 10
    _one_open_per_agent(db)
    assert len(_open_by_agent(db, "Paris")) == 10
    assert orphans(db) == []


def test_more_copies_than_the_judge_reads_are_closed_and_given_back_only_as_read(world, db):
    """The declared limit (docs/guide/limits.md): the judge of a write reads
    a bounded set of candidates (`memory_core.candidates`), so Zanzibar
    closes 10 of 25 copies, and the deletion gives back those 10 and says
    so: one open Paris per agent after it."""
    w = world
    agents = [f"agent-{i:02d}" for i in range(25)]
    new = _paris_in(w, agents)
    assert len(_open_by_agent(db, "Paris")) == 15
    assert _delete(w, new, False)["facts_reopened"] == 10
    _one_open_per_agent(db)
    assert sorted(a for a, _ in _open_by_agent(db, "Paris")) == agents
    assert orphans(db) == []


def test_a_write_landing_in_the_middle_of_the_give_back_leaves_one_open_copy_per_agent(
        world, db, monkeypatch):
    """Agent b is told Paris again between the checks and the insert of its
    copy (a commit of another request): the one-open-row index refuses the
    copy, the savepoint keeps the rest of the give-back, a's copy stands."""
    from korely_agent.services.facts import given_back

    w = world
    new = _paris_in(w, ["a", "b", "d"])
    real = given_back._continuation

    def interfering(f, *a):
        row = real(f, *a)
        if f.agent_id == "b":
            rival = real(f, *a)
            db.add(rival)
            db.flush()
        return row

    monkeypatch.setattr(given_back, "_continuation", interfering)
    assert _delete(w, new, False)["facts_reopened"] == 2
    _one_open_per_agent(db)
    assert sorted(a for a, _ in _open_by_agent(db, "Paris")) == ["a", "b", "d"]
    for a in ("a", "b", "d"):
        assert _overlapping([f for f in _paris(db) if f.agent_id == a]) == []
    assert orphans(db) == []


def test_a_sibling_told_again_in_other_letters_is_the_same_value(world, db):
    """b says "PARIS" after Zanzibar closed its copy: erasing Zanzibar gives
    a its copy back, and b keeps the one open row it has."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.reads["Paris again."] = [Fact("u1", "lives_in", "PARIS")]
    w.write(OLD, agent="a")
    w.write(OLD, agent="b")
    w.stale = ("Paris",)
    new = w.write(NEW, agent="z")
    w.stale = ("Zanzibar",)
    w.write("Paris again.", agent="b")
    assert _delete(w, new, True)["facts_reopened"] == 1
    db.expire_all()
    opened = [f.agent_id for f in db.query(AgentFact)
              if f.invalid_at is None and f.object_label.lower() == "paris"]
    assert sorted(opened) == ["a", "b"]
    assert orphans(db) == []


def test_the_give_back_leaves_every_closed_row_as_it_was(world, db):
    """The two clocks: the rows that were closed keep their end, their
    closing time and their closer; the copies are new rows, not an edit."""
    w = world
    new = _paris_in(w, ["a", "b", "d"])
    db.expire_all()
    before = {str(f.id): (f.valid_from, f.invalid_at, f.closed_known_at, f.invalidated_by_id)
              for f in _paris(db)}
    assert len(before) == 3
    assert _delete(w, new, False)["facts_reopened"] == 3
    after = _paris(db)
    assert len(after) == 6
    for f in after:
        if str(f.id) in before:
            assert (f.valid_from, f.invalid_at, f.closed_known_at, f.invalidated_by_id) \
                == before[str(f.id)]
    assert orphans(db) == []


def test_the_facts_read_shows_one_paris_per_agent_after_the_deletion(world, db):
    w = world
    new = _paris_in(w, ["a", "b"])
    assert len(_facts(w)) == 1
    assert _delete(w, new, False)["facts_reopened"] == 2
    assert len(_facts(w)) == 2
    for a in ("a", "b"):
        assert len(_facts(w, agent_id=a)) == 1


@pytest.mark.parametrize("erase", [False, True], ids=["forget", "erase"])
def test_the_closer_deleted_after_one_agent_was_erased_gives_back_to_the_rest(world, db, erase):
    w = world
    new = _paris_in(w, ["a", "b", "d"])
    r = w.client.delete("/v1/agents/b")
    assert r.status_code == 200, r.text
    out = _delete(w, new, erase)
    assert out["facts_reopened"] == 2
    _one_open_per_agent(db)
    assert sorted(a for a, _ in _open_by_agent(db, "Paris")) == ["a", "d"]
    assert orphans(db) == []
