"""A memory that is forgotten or erased no longer decides what is true.

Found while backing the Cognee #3766 / #5473 claims (deletion leaving the
store inconsistent): the judge closes an older fact when a newer memory
contradicts it. If that newer memory is then forgotten or erased, the older
memory is still live and still says its words, but its fact stayed closed:
the end user had no open fact for what their live memory states, and the
erased memory still shaped what was read.

The founder's decision (31 Oct 2026, services/facts/given_back.py): the
closed fact comes back as a NEW open row, stated by its live memories, from
where what closed it stopped standing (its closing, when the closer is
erased; the forget, when it is forgotten), and the closed row stays untouched
as what the store knew in between (two clocks, Graphiti, Rasmussen et al.
2025, section 2.2.3). Only when
nothing else closes it: the closer still stated by another live memory, an
other value open in the same relation, or no live memory of its own keep it
closed.
"""
import time
from datetime import datetime, timezone

import pytest

from korely_agent.db.models import AgentFact
from tests.deletion_world import Fact, orphans

# the `world` fixture
pytest_plugins = ["tests.deletion_world"]

OLD, NEW = "I live in Paris.", "I moved: now I live in Zanzibar."
ROME = "Since last week I live in Rome."
AGAIN = "Zanzibar is where I live, still."


def _open(db):
    db.expire_all()
    return sorted(f.object_label for f in db.query(AgentFact) if f.invalid_at is None)


def _paris(db):
    db.expire_all()
    return sorted((f for f in db.query(AgentFact) if f.object_label == "Paris"),
                  key=lambda f: f.created_at)


def _paris_then_zanzibar(w):
    w.stale = ("Paris",)
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    old = w.write(OLD)
    return old, w.write(NEW)


def _paris_in(w, agents):
    """Paris told to each of `agents`, then Zanzibar (agent z) closing every
    copy; returns Zanzibar's memory id."""
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    for a in agents:
        w.write(OLD, agent=a)
    w.stale = ("Paris",)
    return w.write(NEW, agent="z")


def _delete(w, mid, erase):
    r = w.client.delete(f"/v1/memories/{mid}", params={"erase": str(erase).lower()})
    assert r.status_code == 200, r.text
    return r.json()


def _moment():
    time.sleep(0.05)
    t = datetime.now(timezone.utc)
    time.sleep(0.05)
    return t


def _facts(w, **params):
    r = w.client.get("/v1/facts", params={"user_id": "u1", **params})
    assert r.status_code == 200, r.text
    return r.json()["facts"]


def _zanzibar(db):
    db.expire_all()
    return [f for f in db.query(AgentFact) if f.object_label == "Zanzibar"]


@pytest.mark.parametrize("erase", [False, True], ids=["forget", "erase"])
def test_deleting_the_memory_that_closed_a_fact_reopens_it(world, db, erase):
    """Cognee #3766 / #5473 (consistency after delete): the older fact is
    open again once the memory that closed it is gone, as a new row; the
    closed row is untouched."""
    w = world
    _old, new = _paris_then_zanzibar(w)
    assert _open(db) == ["Zanzibar"]
    (closed,) = _paris(db)
    (g,) = _zanzibar(db)
    was = (closed.invalid_at, closed.closed_known_at, closed.invalidated_by_id, closed.valid_from)
    assert was[2] == g.id
    out = _delete(w, new, erase)
    assert out["facts_reopened"] == 1
    assert _open(db) == ["Paris"]
    f, f2 = _paris(db)
    # F stays as it was learned, the closer it names included (an erased
    # closer's id goes with its row, by the foreign key).
    assert f.id == closed.id
    assert (f.invalid_at, f.closed_known_at, f.valid_from) == (was[0], was[1], was[3])
    assert f.invalidated_by_id == (None if erase else g.id)
    # F' begins where what closed F stopped standing: its closing for an
    # erased closer, the closer's end (the forget) for a forgotten one.
    assert f2.invalid_at is None
    if erase:
        assert f2.valid_from == f.invalid_at
    else:
        (g,) = _zanzibar(db)
        assert g.invalid_at is not None and f2.valid_from == g.invalid_at > f.invalid_at
    assert f2.source_memory_ids == f.source_memory_ids[:1] and f2.observation_count == 1
    assert orphans(db) == []


@pytest.mark.parametrize("erase", [False, True], ids=["forget", "erase"])
def test_the_store_still_knew_it_closed_in_between_and_one_value_holds_at_a_time(world, db, erase):
    """Two clocks: `known_as_of` before the deletion answers Paris closed,
    by Zanzibar; `as_of` answers one place at a time: Paris before the
    closing, then Zanzibar alone until a forget (Paris alone when erased:
    Zanzibar never was), then Paris."""
    from korely_agent.services.ids import fact_public_id

    w = world
    w.stale = ("Paris",)
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.write(OLD)
    before = _moment()
    new = w.write(NEW)
    between = _moment()
    (g,) = _zanzibar(db)
    ahead = [x["id"] for x in _facts(w, as_of=before.isoformat())]
    _delete(w, new, erase)
    after = _moment()
    assert [x["object"] for x in _facts(w)] == ["Paris"]
    # What the store knew before the deletion: Paris closed, by Zanzibar.
    then = [x["object"] for x in _facts(w, known_as_of=between.isoformat())]
    assert then == ([] if erase else ["Zanzibar"])
    known = {x["object"]: x for x in _facts(w, known_as_of=between.isoformat(),
                                            include_invalidated="true")}
    assert known["Paris"]["invalid_at"] is not None
    if not erase:
        assert known["Paris"]["invalidated_by"] == fact_public_id(g.id)
    # Before the closing: the same row as before the deletion, alone.
    assert [x["id"] for x in _facts(w, as_of=before.isoformat())] == ahead
    # Between the closing and the deletion.
    assert [x["object"] for x in _facts(w, as_of=between.isoformat())] == (
        ["Paris"] if erase else ["Zanzibar"])
    # After it.
    assert [x["object"] for x in _facts(w, as_of=after.isoformat())] == ["Paris"]


def test_another_open_value_keeps_it_closed(world, db):
    """(c): "now I live in Rome" open beside Zanzibar (the judge closed
    nothing for it): with Zanzibar forgotten, Paris stays closed. No judge is
    asked on a deletion."""
    w = world
    _old, new = _paris_then_zanzibar(w)
    w.stale = ()
    w.reads[ROME] = [Fact("u1", "lives_in", "Rome")]
    w.write(ROME)
    assert _open(db) == ["Rome", "Zanzibar"]
    calls = len(w.calls)
    out = _delete(w, new, False)
    assert out["facts_reopened"] == 0 and _open(db) == ["Rome"]
    assert len(_paris(db)) == 1
    assert len(w.calls) == calls


def test_the_closer_still_stated_by_another_live_memory_keeps_it_closed(world, db):
    """(a): Zanzibar said again by another memory loses only the forgotten
    source and stays open; Paris stays closed."""
    w = world
    _old, new = _paris_then_zanzibar(w)
    w.reads[AGAIN] = [Fact("u1", "lives_in", "Zanzibar")]
    w.write(AGAIN)
    for erase in (False, True):
        out = _delete(w, new, erase)
        assert out["facts_reopened"] == 0
        assert _open(db) == ["Zanzibar"] and len(_paris(db)) == 1


def test_paris_with_no_live_memory_is_not_given_back(world, db):
    """(b): the memory that said Paris forgotten first: nothing still says
    it, and forgetting Zanzibar gives nothing back."""
    w = world
    old, new = _paris_then_zanzibar(w)
    _delete(w, old, False)
    out = _delete(w, new, False)
    assert out["facts_reopened"] == 0 and _open(db) == [] and len(_paris(db)) == 1
    assert orphans(db) == []


def test_erasing_the_end_user_gives_nothing_back(world, db):
    w = world
    _paris_then_zanzibar(w)
    r = w.client.delete("/v1/users/u1/memories")
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.query(AgentFact).count() == 0


def test_forgetting_twice_or_erasing_after_gives_back_once(world, db):
    """Idempotent: the second forget finds nothing, and the erasure after it
    finds Paris already open again (condition (c)). The start the forget gave
    stays (CHANGELOG): between the move and the forget, no place."""
    w = world
    _old, new = _paris_then_zanzibar(w)
    between = _moment()
    assert _delete(w, new, False)["facts_reopened"] == 1
    assert w.client.delete(f"/v1/memories/{new}").status_code == 404
    assert _delete(w, new, True)["facts_reopened"] == 0
    assert _open(db) == ["Paris"] and len(_paris(db)) == 2
    assert _facts(w, as_of=between.isoformat()) == []
    assert orphans(db) == []


def test_editing_the_closer_away_gives_it_back(world, db):
    """An edit whose new text no longer says Zanzibar retires it, as a forget
    does: Paris is true again, beside what the new text says."""
    w = world
    _old, new = _paris_then_zanzibar(w)
    w.reads["I like cats."] = [Fact("u1", "likes", "cats")]
    r = w.client.patch(f"/v1/memories/{new}", json={"content": "I like cats."})
    assert r.status_code == 200, r.text
    assert _open(db) == ["Paris", "cats"]
    f, f2 = _paris(db)
    # Zanzibar is history up to the new text's arrival, Paris from there.
    (g,) = _zanzibar(db)
    assert g.invalid_at is not None and f2.valid_from == g.invalid_at


def test_deleting_the_agent_whose_fact_closed_another_agents_gives_it_back(world, db):
    """The judge's candidates are the end user's, of any agent: agent b's
    Zanzibar closed agent a's Paris, and deleting agent b gives it back."""
    w = world
    w.stale = ("Paris",)
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.write(OLD, agent="a")
    w.write(NEW, agent="b")
    assert _open(db) == ["Zanzibar"]
    r = w.client.delete("/v1/agents/b")
    assert r.status_code == 200, r.text
    assert r.json()["facts_reopened"] == 1
    assert _open(db) == ["Paris"]
    f, f2 = _paris(db)
    # Zanzibar went with its namespace: it never was, and Paris goes on from
    # its closing.
    assert f2.agent_id == "a" and f2.valid_from == f.invalid_at
    assert orphans(db) == []


@pytest.mark.parametrize("erase", [False, True], ids=["forget", "erase"])
def test_the_same_fact_of_two_agents_closed_by_one_memory_comes_back_to_both(world, db, erase):
    """(c): Paris told to agents a and b of one end user is two rows, one per
    agent namespace, and one Zanzibar closes both. Forgetting it gives both
    back: the copy given back to a is the same value, not another one, and
    does not keep b's closed. One open Paris per agent, never two."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.write(OLD, agent="a")
    w.write(OLD, agent="b")
    assert sorted(f.agent_id for f in _paris(db) if f.invalid_at is None) == ["a", "b"]
    w.stale = ("Paris",)
    new = w.write(NEW, agent="c")
    assert _open(db) == ["Zanzibar"]
    assert _delete(w, new, erase)["facts_reopened"] == 2
    assert _open(db) == ["Paris", "Paris"]
    paris = _paris(db)
    assert sorted(f.agent_id for f in paris if f.invalid_at is None) == ["a", "b"]
    for agent in ("a", "b"):
        assert _overlapping([f for f in paris if f.agent_id == agent]) == []
    assert orphans(db) == []


def test_deleting_the_closers_agent_gives_both_copies_back(world, db):
    """Agent z's Zanzibar closed the Paris of a and of b: deleting agent z
    gives back each copy, and `facts_reopened` counts both."""
    w = world
    _paris_in(w, ["a", "b"])
    assert _open(db) == ["Zanzibar"]
    r = w.client.delete("/v1/agents/z")
    assert r.status_code == 200, r.text
    assert r.json()["facts_reopened"] == 2
    assert _open(db) == ["Paris", "Paris"]
    paris = _paris(db)
    assert sorted(f.agent_id for f in paris if f.invalid_at is None) == ["a", "b"]
    for agent in ("a", "b"):
        assert _overlapping([f for f in paris if f.agent_id == agent]) == []
    assert orphans(db) == []


def test_editing_the_closer_away_gives_both_copies_back(world, db):
    """An edit of Zanzibar that no longer says it gives the Paris of a and
    of b back, beside what the new text says."""
    w = world
    new = _paris_in(w, ["a", "b"])
    w.stale = ()
    w.reads["I like cats."] = [Fact("u1", "likes", "cats")]
    r = w.client.patch(f"/v1/memories/{new}", json={"content": "I like cats."})
    assert r.status_code == 200, r.text
    assert _open(db) == ["Paris", "Paris", "cats"]
    paris = _paris(db)
    assert sorted(f.agent_id for f in paris if f.invalid_at is None) == ["a", "b"]
    for agent in ("a", "b"):
        assert _overlapping([f for f in paris if f.agent_id == agent]) == []
    assert orphans(db) == []


def test_a_copy_told_again_in_another_agent_does_not_keep_this_one_closed(world, db, monkeypatch):
    """(c) reads F's own triple in any agent as the same value: Paris told to
    agents a and b, Zanzibar (agent z) closes both, b says Paris again (and
    closes Zanzibar). Erasing Zanzibar gives Paris back to a, where nothing
    open says otherwise. b's closed copy is not given back beside its open
    one: (d) finds it, before a row is built for the one-open-row index to
    refuse. (A forget would retract nothing: it ends the memory's open facts,
    and Zanzibar ended when b said Paris again.)"""
    from korely_agent.services.facts import given_back

    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.reads["Paris again."] = [Fact("u1", "lives_in", "Paris")]
    w.write(OLD, agent="a")
    w.write(OLD, agent="b")
    w.stale = ("Paris",)
    new = w.write(NEW, agent="z")
    w.stale = ("Zanzibar",)
    w.write("Paris again.", agent="b")
    assert _open(db) == ["Paris"]
    built = []
    real = given_back._continuation
    monkeypatch.setattr(given_back, "_continuation",
                        lambda f, *a: built.append(f.agent_id) or real(f, *a))
    assert _delete(w, new, True)["facts_reopened"] == 1
    assert built == ["a"]
    paris = _paris(db)
    assert sorted(f.agent_id for f in paris if f.invalid_at is None) == ["a", "b"]
    assert [f["agent_id"] for f in _facts(w, agent_id="a")] == ["a"]
    for agent in ("a", "b"):
        assert _overlapping([f for f in paris if f.agent_id == agent]) == []
    assert orphans(db) == []


def test_two_different_values_closed_by_one_closer_give_back_one(world, db):
    """(c) leaves out F's own triple only: Paris in agent a and Rome in agent
    b, one Zanzibar closes both. Forgetting it gives back Paris, the older,
    and Paris then stands in lives_in, so Rome stays closed: one place open
    for the end user, never two."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[ROME] = [Fact("u1", "lives_in", "Rome")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.write(OLD, agent="a")
    w.write(ROME, agent="b")
    w.stale = ("Paris", "Rome")
    new = w.write(NEW, agent="c")
    assert _open(db) == ["Zanzibar"]
    assert _delete(w, new, False)["facts_reopened"] == 1
    assert _open(db) == ["Paris"]
    assert orphans(db) == []


def test_erasing_a_memory_forgotten_before_the_upgrade_gives_it_back(world, db, monkeypatch):
    """A memory forgotten before this release left Paris closed; its
    erasure now gives Paris back (CHANGELOG, Unreleased)."""
    from korely_agent.services.facts import erasure

    w = world
    _old, new = _paris_then_zanzibar(w)
    real = erasure._give_back
    monkeypatch.setattr(erasure, "_give_back", lambda *a, **k: 0)   # the release before
    _delete(w, new, False)
    assert _open(db) == []
    monkeypatch.setattr(erasure, "_give_back", real)
    assert _delete(w, new, True)["facts_reopened"] == 1
    assert _open(db) == ["Paris"]
    f, f2 = _paris(db)
    assert f2.valid_from == f.invalid_at   # Zanzibar erased: it never was
    assert orphans(db) == []


def _overlapping(rows):
    """The pairs of rows true together on some date."""
    far = datetime.max.replace(tzinfo=timezone.utc)
    return [(a.id, b.id) for a in rows for b in rows if a.id < b.id
            and max(a.valid_from, b.valid_from) < min(a.invalid_at or far, b.invalid_at or far)]


def test_a_fact_stated_by_hand_and_closed_by_a_memory_is_given_back(world, db):
    """(b): POST /v1/facts has no source memory, and the developer's
    statement still stands once the memory that closed it is forgotten."""
    w = world
    r = w.client.post("/v1/facts", json={"subject": "u1", "predicate": "lives_in",
                                         "object": "Paris", "user_id": "u1"})
    assert r.status_code == 201, r.text
    w.stale = ("Paris",)
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    new = w.write(NEW)
    assert _open(db) == ["Zanzibar"]
    assert _delete(w, new, False)["facts_reopened"] == 1
    assert _open(db) == ["Paris"]
    f, f2 = _paris(db)
    assert f2.source_memory_id is None and not f2.source_memory_ids
    assert orphans(db) == []


def test_a_source_edited_since_the_closing_does_not_vouch(world, db):
    """(b): the Paris memory edited to say something else after Paris closed
    (the edit's retirement leaves the closed fact its source): it no longer
    says Paris, and Paris stays closed."""
    w = world
    old, new = _paris_then_zanzibar(w)
    w.reads["I like cats."] = [Fact("u1", "likes", "cats")]
    r = w.client.patch(f"/v1/memories/{old}", json={"content": "I like cats."})
    assert r.status_code == 200, r.text
    assert _open(db) == ["Zanzibar", "cats"]
    assert _delete(w, new, False)["facts_reopened"] == 0
    assert _open(db) == ["cats"] and len(_paris(db)) == 1


def test_a_past_statement_added_to_the_closed_fact_keeps_it_closed(world, db):
    """(b): "I used to live in Paris", told after the move, joins the closed
    row as its history (memory_core `writing._the_past`): it says Paris is
    over whatever closed it, so forgetting Zanzibar gives nothing back."""
    w = world
    _old, new = _paris_then_zanzibar(w)
    used = "I used to live in Paris."
    w.stale = ()
    w.reads[used] = [Fact("u1", "lives_in", "Paris", tense="past")]
    w.write(used)
    (f,) = _paris(db)
    assert len(f.source_memory_ids) == 2
    assert _delete(w, new, False)["facts_reopened"] == 0
    assert _open(db) == [] and len(_paris(db)) == 1


def test_a_fact_given_back_and_ended_since_is_not_given_back_again(world, db):
    """(d): Paris given back at the forget, then forgotten by hand; erasing
    the Zanzibar memory afterwards finds the closed Paris again, and the
    triple's later history (the row given back, ended) keeps it closed: no
    second row beside it, nothing open after the end somebody gave it."""
    from korely_agent.services.ids import fact_public_id

    w = world
    _old, new = _paris_then_zanzibar(w)
    assert _delete(w, new, False)["facts_reopened"] == 1
    _f, f2 = _paris(db)
    r = w.client.post(f"/v1/facts/{fact_public_id(f2.id)}/forget")
    assert r.status_code == 200, r.text
    assert _open(db) == []
    assert _delete(w, new, True)["facts_reopened"] == 0
    paris = _paris(db)
    assert _open(db) == [] and len(paris) == 2 and _overlapping(paris) == []
    assert orphans(db) == []


def test_a_closer_dated_ahead_and_forgotten_gives_it_back_from_its_end(world, db):
    """A closer dated ahead closes Paris at that future instant; forgotten
    before it, Paris goes on from there, by a new row that begins where the
    old one ends: Paris true now, and after that date, once."""
    from datetime import timedelta

    from sqlalchemy import text

    w = world
    _old, new = _paris_then_zanzibar(w)
    (f,) = _paris(db)
    (g,) = _zanzibar(db)
    ahead = datetime.now(timezone.utc) + timedelta(hours=2)
    # The judge closes the older fact where the newer begins (memory_core
    # `judge.close`): as a write dated two hours ahead would have left them.
    db.execute(text("UPDATE agent_facts SET valid_from = :t WHERE id = :g"), {"t": ahead, "g": g.id})
    db.execute(text("UPDATE agent_facts SET invalid_at = :t WHERE id = :f"), {"t": ahead, "f": f.id})
    db.commit()
    assert _delete(w, new, False)["facts_reopened"] == 1
    f, f2 = _paris(db)
    assert f.invalid_at == ahead and f2.valid_from == ahead and f2.invalid_at is None
    assert _overlapping([f, f2]) == []
    assert [x["object"] for x in _facts(w)] == ["Paris"]
    later = (ahead + timedelta(hours=1)).isoformat()
    assert [x["object"] for x in _facts(w, as_of=later)] == ["Paris"]


@pytest.mark.parametrize("erase", [False, True], ids=["forget", "erase"])
def test_the_mcp_forget_says_what_came_back_as_rest_does(world, db, tenant, erase):
    """korely_forget_memory is DELETE /v1/memories/{id}'s twin
    (tests/test_mcp_correct_and_forget.py): the fact it gives back is in its
    answer, for the agent to read again, and in the trail as a count."""
    import asyncio

    from korely_agent.db.models import AuditEvent
    from korely_agent.mcp import server as mcp_server

    w = world
    _old, new = _paris_then_zanzibar(w)
    key = w.client.headers["Authorization"].split(" ", 1)[1]

    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(key) is not None
        out = await mcp_server._get_agent_mcp().call_tool(
            "korely_forget_memory", {"memory_id": new, "erase": erase})
        content = out[0] if isinstance(out, tuple) else out
        return "".join(c.text for c in content)

    said = asyncio.run(go())
    assert "Facts true again: 1." in said
    assert _open(db) == ["Paris"]
    db.expire_all()
    (row,) = [r for r in db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id)
              if (r.meta or {}).get("endpoint") == "memories:delete"]
    assert row.meta["facts_reopened"] == 1
    assert "Paris" not in str(row.meta) and "Zanzibar" not in str(row.meta)
