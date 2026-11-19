"""A fact somebody said is over stays over when the successor the judge had
named for an end ahead goes (code review finding, P1, 8 Nov 2026; the
skeptic's rounds of 9 Nov 2026).

"I live in Paris" gives F. "From <tomorrow> I live in Zanzibar" gives G,
which begins tomorrow, and the judge closes F there, `invalidated_by_id` = G
(memory_core `judge.close`). Then F is ended earlier, in place: the caller
forgets it (POST /v1/facts/{id}/forget), corrects it, or posts it in the past
tense (POST /v1/facts); or a later memory says "I no longer live in Paris";
or F's own memory is edited into a text that no longer states it. Each
brings the end earlier and leaves the pointer to G. Until 9 Nov 2026,
deleting Zanzibar's memory then gave Paris back: rule (a) of
facts/given_back.py read the pointer as what had closed F.

Now who ended F in place is written when it happens, and the deletion reads
only that and the pointers, never the clocks of the rows: the caller's word
is a record in agent_forget_audit (`given_back.record_by_hand`), a memory's
is a row of agent_fact_ended_by for each memory that says it
(`given_back.said_over`, facts/previous_version.py). A fact the caller said
is over never comes back; one memories ended in place comes back once every
one of them, and what else closed it, is gone. The same with the memories written one by one, waiting
together for the worker (the default, WRITE_ASYNC on), or in one import
(POST /v1/batch).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from korely_agent.db.models import AgentFact, AgentFactEndedBy, AgentForgetAudit, AgentMemory
from korely_agent.services.ids import fact_public_id, mem_public_id
from tests.deletion_world import Fact, orphans
from tests.test_deleting_a_memory_gives_back_what_it_closed import (
    OLD, _delete, _facts, _moment, _open, _overlapping, _paris, _zanzibar)

# the `world` fixture
pytest_plugins = ["tests.deletion_world"]

NEW = "Now I live in Zanzibar."
CATS = "I like cats."
OVER = "I no longer live in Paris."
ISLAND = "Zanzibar Island is home."

#: How the memory that said Zanzibar goes: forgotten, erased, edited so that
#: it no longer says it, or with its agent's namespace.
RETRACTIONS = ["forget", "erase", "edit", "agent"]

#: How the later memories reach the store: one by one with their facts in the
#: answer (the world's WRITE_ASYNC off), waiting together for the worker
#: (WRITE_ASYNC on, the default), or in one import (POST /v1/batch).
MODES = ["one_by_one", "waiting_together", "one_import"]


def _tomorrow():
    """Midnight UTC of tomorrow, the latest start a date in a memory's words
    gives (memory_core `fact_time.date_from_text`: whole days, at most one
    ahead). Skipped in the ten minutes before it, when it could come during
    the test."""
    now = datetime.now(timezone.utc)
    day = (now + timedelta(days=1)).date()
    ahead = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    if ahead - now < timedelta(minutes=10):
        pytest.skip("ten minutes before midnight UTC: tomorrow could come during the test")
    return ahead


def _queued(monkeypatch, on):
    from korely_agent.config import get_settings

    monkeypatch.setenv("WRITE_ASYNC", "true" if on else "false")
    get_settings.cache_clear()


def _memory_id(db, content):
    db.expire_all()
    (m,) = [m for m in db.query(AgentMemory) if m.content == content]
    return mem_public_id(m.id)


def _say(w, monkeypatch, mode, items):
    """The memories `items` ((text, user, agent, extra)) written `mode`, the
    judge closing what names Paris; their ids."""
    from korely_agent.services.batch import process_batch_jobs

    w.stale = ("Paris",)
    try:
        if mode == "one_by_one":
            return [w.write(t, user=u, agent=a, **x) for t, u, a, x in items]
        _queued(monkeypatch, True)
        try:
            if mode == "waiting_together":
                ids = [w.write(t, user=u, agent=a, **x) for t, u, a, x in items]
            else:
                body = [{"content": t, "user_id": u, **({"agent_id": a} if a else {}), **x}
                        for t, u, a, x in items]
                r = w.client.post("/v1/batch", json={"memories": body})
                assert r.status_code == 202, r.text
                process_batch_jobs(w.db)
                ids = [_memory_id(w.db, t) for t, _u, _a, _x in items]
            w.drain()
        finally:
            _queued(monkeypatch, False)
        return ids
    finally:
        w.stale = ()


def _closed_ahead(w, *, agents=False, paris_said=None, monkeypatch=None, mode="one_by_one",
                  then=()):
    """Paris, then "From <tomorrow> I live in Zanzibar": the model reads the
    date in the words, Zanzibar begins tomorrow, and the judge closes Paris
    there in Zanzibar's write; then the memories `then` ((text, extra)), in
    the agent of Paris. Zanzibar and those are written `mode`. No row is
    touched by hand. Returns (Zanzibar's memory id, the ids of `then`,
    tomorrow)."""
    ahead = _tomorrow()
    said = f"From {ahead.date().isoformat()} I live in Zanzibar."
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[said] = [Fact("u1", "lives_in", "Zanzibar", valid_from=ahead.date().isoformat())]
    w.write(OLD, agent="a" if agents else None,
            **({"timestamp": paris_said} if paris_said else {}))
    ids = _say(w, monkeypatch, mode, [(said, "u1", "z" if agents else None, {})]
               + [(t, "u1", "a" if agents else None, x) for t, x in then])
    (g,) = _zanzibar(w.db)
    assert g.valid_from == ahead
    if not then:
        (f,) = _paris(w.db)
        assert f.invalid_at == ahead and f.invalidated_by_id == g.id
    return ids[0], ids[1:], ahead


def _retract(w, mid, how):
    """The memory `mid` goes, `how`; the answer's facts_reopened (None for an
    edit, whose answer does not count them)."""
    if how in ("forget", "erase"):
        return _delete(w, mid, how == "erase")["facts_reopened"]
    if how == "edit":
        w.reads[CATS] = [Fact("u1", "likes", "cats")]
        r = w.client.patch(f"/v1/memories/{mid}", json={"content": CATS})
        assert r.status_code == 200, r.text
        return None
    r = w.client.delete("/v1/agents/z")
    assert r.status_code == 200, r.text
    return r.json()["facts_reopened"]


def _edit(w, monkeypatch, mid, content, facts, mode="one_by_one"):
    """PATCH /v1/memories/{mid} to `content`, read as `facts`, written
    `mode` (one by one, or waiting for the worker)."""
    w.reads[content] = facts
    if mode == "one_by_one":
        r = w.client.patch(f"/v1/memories/{mid}", json={"content": content})
        assert r.status_code == 200, r.text
        return
    _queued(monkeypatch, True)
    try:
        r = w.client.patch(f"/v1/memories/{mid}", json={"content": content})
        assert r.status_code == 200, r.text
        w.drain()
    finally:
        _queued(monkeypatch, False)


def _forget(w, fact_id, at=None):
    """POST /v1/facts/{id}/forget; its answer."""
    r = w.client.post(f"/v1/facts/{fact_public_id(fact_id)}/forget",
                      json={"at": at if isinstance(at, str) else at.isoformat()}
                      if at is not None else None)
    assert r.status_code == 200, r.text
    return r.json()


def _paris_at(w, when=None):
    """Whether Paris reads true at `when` (now when None)."""
    params = {"as_of": when.isoformat()} if when is not None else {}
    return "Paris" in [x["object"] for x in _facts(w, **params)]


def _enders(db, f):
    """The memories that ended `f` in place (agent_fact_ended_by)."""
    return {e.memory_id for e in db.query(AgentFactEndedBy).filter(AgentFactEndedBy.fact_id == f.id)}


def _uuid(public):
    from korely_agent.services.ids import parse_mem_id

    return parse_mem_id(public)


# ── the finding: a forget before the end the judge gave ────────────────────

@pytest.mark.parametrize("how", RETRACTIONS)
@pytest.mark.parametrize("when", ["now", "earlier", "ahead"])
def test_a_fact_forgotten_before_the_end_the_judge_gave_it_never_comes_back(world, db, how, when):
    """The finding's case (the forget brings the end to now), a forget dated
    earlier, and one dated between now and the day Zanzibar begins: every
    door that retracts Zanzibar's memory leaves Paris forgotten, at every
    moment after the end the caller gave it."""
    w = world
    new, _, ahead = _closed_ahead(w, agents=how == "agent")
    (f,) = _paris(db)
    now = datetime.now(timezone.utc)
    at = {"now": None, "earlier": _moment(), "ahead": now + (ahead - now) / 2}[when]
    assert _forget(w, f.id, at)["status"] == "forgotten"
    (f,) = _paris(db)
    (g,) = _zanzibar(db)
    # The pointer stays on the row.
    assert f.invalid_at < g.valid_from and f.invalidated_by_id == g.id
    was = (f.invalid_at, f.closed_known_at, f.valid_from)
    assert _retract(w, new, how) in (0, None)
    paris = _paris(db)
    assert len(paris) == 1 and "Paris" not in _open(db)
    (f,) = paris
    # Untouched by the retraction, but for the pointer of a closer that is
    # deleted (the foreign key's SET NULL).
    assert (f.invalid_at, f.closed_known_at, f.valid_from) == was
    assert f.invalidated_by_id == (None if how in ("erase", "agent") else g.id)
    assert not _paris_at(w, f.invalid_at + timedelta(seconds=1))
    assert not _paris_at(w, ahead + timedelta(days=1))
    assert orphans(db) == []


@pytest.mark.parametrize("kind", ["equal", "equal_in_another_zone", "after"])
def test_a_forget_at_or_after_the_end_ahead_keeps_it_forgotten(world, db, kind):
    """A forget whose `at` is at or after the end the judge gave answers
    "already_forgotten": the fact ends by then, at the judge's date, and a
    forget never moves an end later (service.forget_fact). It is the
    caller's word all the same: retracting Zanzibar's memory gives nothing
    back, and Paris is not true after the caller's `at`."""
    w = world
    new, _, ahead = _closed_ahead(w)
    (f,) = _paris(db)
    at = {"equal": ahead, "equal_in_another_zone":
          ahead.astimezone(timezone(timedelta(hours=5, minutes=30))),
          "after": ahead + timedelta(days=5)}[kind]
    out = _forget(w, f.id, at)
    assert out["status"] == "already_forgotten"
    assert datetime.fromisoformat(out["invalid_at"]) == ahead
    assert _delete(w, new, False)["facts_reopened"] == 0
    assert "Paris" not in _open(db) and len(_paris(db)) == 1
    assert not _paris_at(w, at + timedelta(days=1))


def test_a_forget_that_found_the_fact_already_closed_keeps_it_forgotten(world, db):
    """A forget of a fact the judge had closed in the past moves no end
    ("already_forgotten"): it is the caller's word too, and retracting the
    memory that closed it gives nothing back. Until 9 Nov 2026 it did."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.write(OLD)
    w.stale = ("Paris",)
    new = w.write(NEW)
    (f,) = _paris(db)
    assert _forget(w, f.id)["status"] == "already_forgotten"
    assert _delete(w, new, False)["facts_reopened"] == 0
    assert _open(db) == []


def test_a_fact_corrected_by_hand_is_not_given_back(world, db):
    """A correction by hand (PATCH /v1/facts/{id}) closes Paris by its new
    value, which carries Paris's memory. Paris is also stated by a second
    memory; forgetting the first retracts the corrected value, and the
    second memory would vouch for Paris: the caller's correction keeps it
    closed."""
    w = world
    again = "Paris is where I live."
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[again] = [Fact("u1", "lives_in", "Paris")]
    first = w.write(OLD)
    w.write(again)
    (f,) = _paris(db)
    assert len(f.source_memory_ids) == 2
    r = w.client.patch(f"/v1/facts/{fact_public_id(f.id)}", json={"object": "Lyon"})
    assert r.status_code == 200, r.text
    assert _delete(w, first, False)["facts_reopened"] == 0
    assert "Paris" not in _open(db)


def _records(db, f):
    """The caller's records on `f`: (scope, facts_invalidated), oldest first."""
    db.expire_all()
    return [(a.scope, a.facts_invalidated) for a in
            db.query(AgentForgetAudit).order_by(AgentForgetAudit.created_at)
            if a.target_id == fact_public_id(f.id)]


@pytest.mark.parametrize("how", RETRACTIONS)
@pytest.mark.parametrize("when", ["before_the_day_ahead", "after_the_closing"])
def test_a_past_fact_posted_by_hand_is_the_callers_word(world, db, when, how):
    """POST /v1/facts with tense 'past' on Paris: memory_core
    `writing._the_past` closes the row it restates when it is open (the
    judge's end still ahead), or adds to the closed row it is history of.
    It is the caller saying Paris is over, recorded in the forgets' table
    with a scope of its own, 'past', which is not a forget's 'fact' (1 when
    it closed it, 0 when Paris was over already), and every door that
    retracts Zanzibar's memory gives nothing back."""
    w = world
    agents = how == "agent"
    if when == "before_the_day_ahead":
        new, _, ahead = _closed_ahead(w, agents=agents)
    else:
        w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
        w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
        w.write(OLD, agent="a" if agents else None)
        w.stale = ("Paris",)
        new = w.write(NEW, agent="z" if agents else None)
        w.stale = ()
        ahead = datetime.now(timezone.utc)
    r = w.client.post("/v1/facts", json={"subject": "u1", "predicate": "lives_in",
                                         "object": "Paris", "user_id": "u1", "tense": "past",
                                         **({"agent_id": "a"} if agents else {})})
    assert r.status_code == 201, r.text
    (f,) = _paris(db)
    (g,) = _zanzibar(db)
    # Closed in place it takes the past tense; history, it stays as it was.
    assert f.tense == ("past" if when == "before_the_day_ahead" else "current")
    assert f.invalidated_by_id == g.id and _enders(db, f) == set()
    assert _records(db, f) == [("past", 1 if when == "before_the_day_ahead" else 0)]
    assert _retract(w, new, how) in (0, None)
    assert len(_paris(db)) == 1 and "Paris" not in _open(db)
    assert not _paris_at(w, ahead + timedelta(days=1))


def test_a_new_past_fact_posted_by_hand_writes_no_record(world, db):
    """A past fact posted by hand that lands on no row of Paris is a new row
    of history: nothing the store held was said over, and no record is
    written."""
    w = world
    r = w.client.post("/v1/facts", json={"subject": "u1", "predicate": "lives_in",
                                         "object": "Paris", "user_id": "u1", "tense": "past"})
    assert r.status_code == 201, r.text
    (f,) = _paris(db)
    assert f.tense == "past" and _records(db, f) == []


def test_a_past_fact_posted_by_hand_is_told_apart_from_a_forget(world, db):
    """Through the doors a caller uses: "I live in Paris", then POST
    /v1/facts with tense 'past' on it, which closes it (scope 'past', 1),
    then a forget of the same fact, already over (scope 'fact', 0). A reader
    of the records tells the two apart: the past statement is no forget.
    Nothing else closed Paris, and it stays closed."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.write(OLD)
    r = w.client.post("/v1/facts", json={"subject": "u1", "predicate": "lives_in",
                                         "object": "Paris", "user_id": "u1", "tense": "past"})
    assert r.status_code == 201, r.text
    (f,) = _paris(db)
    assert f.tense == "past" and f.invalid_at is not None
    assert _records(db, f) == [("past", 1)]
    assert _forget(w, f.id)["status"] == "already_forgotten"
    assert _records(db, f) == [("past", 1), ("fact", 0)]
    assert "Paris" not in _open(db)


def test_a_current_fact_posted_by_hand_on_one_held_writes_no_record(world, db):
    """POST /v1/facts in the present tense on a fact a memory holds restates
    it (memory_core reconfirms the row): nobody said it is over, no record
    is written, and deleting what later closed it gives it back."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.write(OLD)
    r = w.client.post("/v1/facts", json={"subject": "u1", "predicate": "lives_in",
                                         "object": "Paris", "user_id": "u1"})
    assert r.status_code == 201, r.text
    (f,) = _paris(db)
    assert _records(db, f) == []
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.stale = ("Paris",)
    new = w.write(NEW)
    w.stale = ()
    assert "Paris" not in _open(db)
    assert _delete(w, new, False)["facts_reopened"] == 1
    assert "Paris" in _open(db)


# ── what does not change: an end ahead nobody brought earlier ──────────────

@pytest.mark.parametrize("how", RETRACTIONS)
def test_an_end_ahead_nobody_brought_earlier_is_given_back_from_it(world, db, how):
    """The judge's end ahead, not forgotten, is the closing, and retracting
    Zanzibar's memory gives Paris back from there: true now by F, after that
    day by F', once."""
    w = world
    new, _, ahead = _closed_ahead(w, agents=how == "agent")
    assert _retract(w, new, how) in (1, None)
    f, f2 = _paris(db)
    assert f.invalid_at == ahead and f2.valid_from == ahead and f2.invalid_at is None
    assert f2.agent_id == f.agent_id and _overlapping([f, f2]) == []
    assert _paris_at(w) and _paris_at(w, ahead + timedelta(hours=1))
    assert orphans(db) == []


@pytest.mark.parametrize("forgotten", [False, True], ids=["kept", "forgotten"])
def test_a_fact_posted_ten_days_ahead_in_an_agent_namespace(world, db, forgotten):
    """Ten days ahead, with no hand on the rows: Zanzibar posted by hand in
    agent z (POST /v1/facts, `valid_from`), and the judge closes Paris
    there. Deleting agent z gives Paris back from that day, unless the
    caller forgot Paris before it."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.write(OLD, agent="a")
    w.stale = ("Paris",)
    ahead = (datetime.now(timezone.utc) + timedelta(days=10)).replace(microsecond=0)
    r = w.client.post("/v1/facts", json={"subject": "u1", "predicate": "lives_in",
                                         "object": "Zanzibar", "user_id": "u1",
                                         "agent_id": "z", "valid_from": ahead.isoformat()})
    assert r.status_code == 201, r.text
    (f,) = _paris(db)
    (g,) = _zanzibar(db)
    assert g.valid_from == ahead and f.invalid_at == ahead and f.invalidated_by_id == g.id
    if forgotten:
        assert _forget(w, f.id)["status"] == "forgotten"
    assert _retract(w, None, "agent") == (0 if forgotten else 1)
    assert _paris_at(w, ahead + timedelta(days=1)) is not forgotten
    assert len(_paris(db)) == (1 if forgotten else 2)


# ── a later memory's past statement ─────────────────────────────────────────

def _said_over(w):
    w.reads[OVER] = [Fact("u1", "lives_in", "Paris", tense="past")]
    return [(OVER, {})]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("how", RETRACTIONS)
def test_a_memory_that_says_it_is_over_before_the_day_ahead_keeps_it_closed(world, db,
                                                                           monkeypatch, how,
                                                                           mode):
    """"I no longer live in Paris", told before the day ahead, closes F in
    place when it is said (memory_core `writing._the_past`) and leaves
    `invalidated_by_id` = G; its write records that memory as who ended F
    (agent_fact_ended_by). Written one by one, or waiting for the worker
    together with Zanzibar's memory, or in one import with it, where it
    arrives before Zanzibar is learned. That memory still says Paris is
    over, and retracting Zanzibar's memory gives nothing back."""
    w = world
    new, (over,), ahead = _closed_ahead(w, agents=how == "agent", monkeypatch=monkeypatch,
                                        mode=mode, then=_said_over(w))
    (f,) = _paris(db)
    (g,) = _zanzibar(db)
    assert f.tense == "past" and f.invalid_at < g.valid_from and f.invalidated_by_id == g.id
    assert _enders(db, f) == {_uuid(over)}
    assert _retract(w, new, how) in (0, None)
    assert len(_paris(db)) == 1 and "Paris" not in _open(db)
    assert not _paris_at(w, ahead + timedelta(days=1))
    assert orphans(db) == []


@pytest.mark.parametrize("erase", [False, True], ids=["forget", "erase"])
def test_a_dated_memory_that_says_it_is_over_keeps_it_closed(world, db, monkeypatch, erase):
    """The same with the memories dated: Paris said on 1 October, "I no
    longer live in Paris" on 1 November, which is where F ends."""
    w = world
    _said_over(w)
    new, (over,), ahead = _closed_ahead(w, paris_said="2026-10-01T00:00:00Z",
                                        monkeypatch=monkeypatch,
                                        then=[(OVER, {"timestamp": "2026-11-01T00:00:00Z"})])
    (f,) = _paris(db)
    assert f.invalid_at == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert _enders(db, f) == {_uuid(over)}
    assert _delete(w, new, erase)["facts_reopened"] == 0
    assert "Paris" not in _open(db) and not _paris_at(w, ahead + timedelta(days=1))


@pytest.mark.parametrize("clock", ["another_zone", "the_clients_clock_ahead"])
def test_the_memory_that_says_it_is_over_is_known_whatever_its_clock(world, db, monkeypatch,
                                                                    clock):
    """Its timestamp in another zone (01:00 +01:00 on 1 November), or a
    client clock five minutes ahead (memory_core `worker.read_event_time`
    brings it to now): the memory is recorded as who ended F all the same,
    and retracting Zanzibar's memory gives nothing back."""
    w = world
    ts = ("2026-11-01T01:00:00+01:00" if clock == "another_zone"
          else (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
    _said_over(w)
    new, (over,), ahead = _closed_ahead(w, paris_said="2026-10-01T00:00:00Z",
                                        monkeypatch=monkeypatch,
                                        then=[(OVER, {"timestamp": ts})])
    (f,) = _paris(db)
    assert _enders(db, f) == {_uuid(over)}
    assert _delete(w, new, False)["facts_reopened"] == 0
    assert "Paris" not in _open(db)


@pytest.mark.parametrize("zanzibar", ["forgotten", "edited"])
@pytest.mark.parametrize("first", ["over", "zanzibar"])
def test_deleting_the_memory_that_said_it_was_over_and_zanzibars(world, db, monkeypatch, first,
                                                                 zanzibar):
    """Both later memories go, in either order, Zanzibar's forgotten or
    edited into a text that no longer says it: whichever goes second gives
    Paris back, since what else closed it is gone already; only "I live in
    Paris" is left. The first gives nothing back: the other still closes it
    (Zanzibar from tomorrow, or the memory that says it is over). Paris is
    true again from tomorrow, where the judge had ended it."""
    w = world
    new, (over,), ahead = _closed_ahead(w, monkeypatch=monkeypatch, then=_said_over(w))
    steps = {"over": lambda: _delete(w, over, False),
             "zanzibar": lambda: _retract(w, new, "forget" if zanzibar == "forgotten" else "edit")}
    open_after = []
    for step in ((["over", "zanzibar"]) if first == "over" else (["zanzibar", "over"])):
        steps[step]()
        open_after.append("Paris" in _open(db))
    assert open_after == [False, True]
    f, f2 = _paris(db)
    assert f2.valid_from == ahead and f2.invalid_at is None and _overlapping([f, f2]) == []
    assert orphans(db) == []


@pytest.mark.parametrize("erase", [False, True], ids=["forget", "erase"])
def test_deleting_the_memory_that_said_it_was_over_gives_it_back(world, db, erase):
    """"I live in Paris", "I no longer live in Paris", nothing else: the
    second memory ended Paris in place, and deleting it gives Paris back,
    from its forget (it said Paris was over until then), or from where
    Paris ended when it is erased (it never said it). Until 1.0.13 Paris
    stayed closed: the store did not record which memory had ended it."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.write(OLD)
    _said_over(w)
    over = w.write(OVER)
    (f,) = _paris(db)
    assert f.tense == "past" and f.invalidated_by_id is None
    assert _enders(db, f) == {_uuid(over)}
    before = datetime.now(timezone.utc)
    assert _delete(w, over, erase)["facts_reopened"] == 1
    f, f2 = _paris(db)
    assert f2.tense == "current" and f2.invalid_at is None
    if erase:
        assert f2.valid_from == f.invalid_at
    else:
        assert f2.valid_from >= before and _overlapping([f, f2]) == []
    assert f2.source_memory_ids == f.source_memory_ids


@pytest.mark.parametrize("dated", ["no_timestamp", "before_the_end"])
def test_a_pointer_a_later_memory_set_is_not_the_closing(world, db, dated):
    """"I no longer live in Paris" (1 November) closes F in place, with no
    pointer. A later memory, "I moved to Zanzibar, I used to live in Paris",
    lands on that closed row as its history and points it at its own
    Zanzibar (memory_core `worker._point_to_the_successor`), even when it is
    dated 20 October, before the end. Both memories say Paris is over and
    are recorded as such, and while the one of 1 November stands deleting
    the later one leaves Paris closed."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.write(OLD, timestamp="2026-10-01T00:00:00Z")
    _said_over(w)
    over = w.write(OVER, timestamp="2026-11-01T00:00:00Z")
    moved = "I moved to Zanzibar, I used to live in Paris."
    w.reads[moved] = [Fact("u1", "lives_in", "Paris", tense="past"),
                      Fact("u1", "lives_in", "Zanzibar")]
    extra = {"timestamp": "2026-10-20T00:00:00Z"} if dated == "before_the_end" else {}
    m2 = w.write(moved, **extra)
    (f,) = _paris(db)
    (z,) = _zanzibar(db)
    assert f.invalidated_by_id == z.id and _enders(db, f) == {_uuid(over), _uuid(m2)}
    assert _delete(w, m2, False)["facts_reopened"] == 0
    assert "Paris" not in _open(db) and not _paris_at(w)


def test_a_value_of_the_same_memory_that_begins_later_still_gives_it_back(world, db):
    """A past statement closes Basic in place when it is said and points it
    at the value of its own write (memory_core
    `worker._point_to_the_successor`), which here begins tomorrow, after
    Basic ended. Both closings are that memory's, and forgetting it gives
    Basic back."""
    w = world
    ahead = _tomorrow()
    basic = "I am on the Basic plan."
    moved = f"From {ahead.date().isoformat()} I am on Pro, I used to be on Basic."
    w.reads[basic] = [Fact("u1", "has_plan", "Basic")]
    w.reads[moved] = [Fact("u1", "has_plan", "Basic", tense="past"),
                      Fact("u1", "has_plan", "Pro", valid_from=ahead.date().isoformat())]
    w.write(basic)
    new = w.write(moved)
    db.expire_all()
    (b,) = [f for f in db.query(AgentFact) if f.object_label == "Basic"]
    (p,) = [f for f in db.query(AgentFact) if f.object_label == "Pro"]
    assert b.tense == "past" and b.invalid_at < p.valid_from == ahead
    assert b.invalidated_by_id == p.id and _enders(db, b) == {_uuid(new)}
    assert _delete(w, new, False)["facts_reopened"] == 1
    assert "Basic" in _open(db)


# ── every memory that says it is over ──────────────────────────────────────

AGAIN = "Paris is not my home any more."


@pytest.mark.parametrize("which", ["first", "second"])
@pytest.mark.parametrize("mode", MODES)
def test_two_memories_that_say_it_is_over_keep_it_closed_until_both_go(world, db, monkeypatch,
                                                                       mode, which):
    """"I live in Paris"; then "I no longer live in Paris" and "Paris is not
    my home any more", and nothing else. Each is recorded as a memory that
    says Paris is over, however they reach the store: one by one (the second
    lands on the row the first closed, as its history), waiting together for
    the worker, or in one import (there the second's text arrived before the
    store learned Paris had ended, and read as vouching for it once the first
    went: the skeptic's fourth round of 9 Nov 2026). Deleting either leaves
    Paris closed, as in 1.0.13; deleting the other too gives it back, from
    that last forget."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.write(OLD)
    _said_over(w)
    w.reads[AGAIN] = [Fact("u1", "lives_in", "Paris", tense="past")]
    over, again = _say(w, monkeypatch, mode, [(OVER, "u1", None, {}), (AGAIN, "u1", None, {})])
    (f,) = _paris(db)
    assert f.tense == "past" and f.invalid_at is not None
    assert _enders(db, f) == {_uuid(over), _uuid(again)}
    first, second = (over, again) if which == "first" else (again, over)
    assert _delete(w, first, False)["facts_reopened"] == 0
    assert "Paris" not in _open(db) and len(_paris(db)) == 1
    before = datetime.now(timezone.utc)
    assert _delete(w, second, False)["facts_reopened"] == 1
    f, f2 = _paris(db)
    assert f2.invalid_at is None and f2.valid_from >= before and _overlapping([f, f2]) == []
    assert orphans(db) == []


@pytest.mark.parametrize("mode", ["one_by_one", "waiting_together"])
def test_a_memory_that_says_it_again_after_the_value_keeps_it_closed(world, db, monkeypatch,
                                                                     mode):
    """"I live in Paris", "Now I live in Zanzibar" (the judge closes Paris),
    then "I used to live in Paris", which lands on the closed row as its
    history: one more memory that says Paris is over, recorded too.
    Deleting Zanzibar's memory leaves Paris closed while it is live, also
    when it waited for the worker with Zanzibar's memory, where its text
    arrived before the store learned Paris had ended."""
    w = world
    used = "I used to live in Paris."
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.reads[used] = [Fact("u1", "lives_in", "Paris", tense="past")]
    w.write(OLD)
    new, said = _say(w, monkeypatch, mode, [(NEW, "u1", None, {}), (used, "u1", None, {})])
    (f,) = _paris(db)
    (g,) = _zanzibar(db)
    assert f.invalidated_by_id == g.id and _enders(db, f) == {_uuid(said)}
    assert _delete(w, new, False)["facts_reopened"] == 0
    assert "Paris" not in _open(db)
    assert _delete(w, said, False)["facts_reopened"] == 1
    assert "Paris" in _open(db)


@pytest.mark.parametrize("how", ["forget", "erase", "edit", "edited_still_saying_it"])
def test_what_a_past_statement_judged_over_comes_back_with_it(world, db, monkeypatch, how):
    """"I live in Paris and I love Paris"; "I no longer live in Paris" ends
    'lives_in' in place, and its judgement (memory_core
    `writing._judge_the_past`: the facts of the same two entities) closes
    'loves', pointing it at the row the statement landed on. Both are that
    memory's word: forgotten, erased, or edited into a text that no longer
    says it, it gives both back. Edited into a text that still says it (a
    typo fixed), it keeps both, and its forget then gives both back. Until
    the skeptic's fourth round only 'lives_in' came back."""
    w = world
    both = "I live in Paris and I love Paris."
    w.reads[both] = [Fact("u1", "lives_in", "Paris"), Fact("u1", "loves", "Paris")]
    w.write(both)
    _said_over(w)
    w.stale = ("Paris",)
    over = w.write(OVER)
    w.stale = ()
    db.expire_all()
    rows = {f.predicate_canonical: f for f in db.query(AgentFact)}
    lives, loves = rows["lives_in"], rows["loves"]
    assert lives.invalid_at is not None and loves.invalidated_by_id == lives.id
    assert _enders(db, lives) == _enders(db, loves) == {_uuid(over)}
    if how == "edit":
        _edit(w, monkeypatch, over, CATS, [Fact("u1", "likes", "cats")])
    else:
        if how == "edited_still_saying_it":
            _edit(w, monkeypatch, over, "I no longer live in Paris!",
                  [Fact("u1", "lives_in", "Paris", tense="past")])
            assert _enders(db, lives) == _enders(db, loves) == {_uuid(over)}
        assert _delete(w, over, how == "erase")["facts_reopened"] == 2
    db.expire_all()
    true = sorted(f.predicate_canonical for f in db.query(AgentFact) if f.invalid_at is None)
    assert true == (["likes", "lives_in", "loves"] if how == "edit" else ["lives_in", "loves"])
    assert orphans(db) == []


def test_what_a_past_statement_judged_over_stays_while_another_says_it(world, db):
    """The same, with a second memory that says 'lives_in' Paris is over
    ("Paris is not my home any more"): deleting the first leaves 'lives_in'
    closed, and 'loves', which follows the row the statement landed on,
    closed with it."""
    w = world
    both = "I live in Paris and I love Paris."
    w.reads[both] = [Fact("u1", "lives_in", "Paris"), Fact("u1", "loves", "Paris")]
    w.write(both)
    _said_over(w)
    w.reads[AGAIN] = [Fact("u1", "lives_in", "Paris", tense="past")]
    w.stale = ("Paris",)
    over = w.write(OVER)
    w.write(AGAIN)
    w.stale = ()
    assert _delete(w, over, False)["facts_reopened"] == 0
    db.expire_all()
    assert [f.predicate_canonical for f in db.query(AgentFact) if f.invalid_at is None] == []


@pytest.mark.parametrize("rome", [False, True], ids=["alone", "rome_open"])
def test_a_row_ended_in_place_weighs_the_relation_of_its_closer(world, db, rome):
    """"I live in Paris"; "From <tomorrow> I left Paris", under a relation
    of its own, which the judge reads beside lives_in Paris (the same two
    entities) and closes it there; "I no longer live in Paris" ends it now,
    in place. The memory of the closer goes (Paris stays: the other memory
    says it is over), then that memory. Paris comes back unless a value
    stands under the closer's relation ("I left Rome too"), as for a closer
    retracted alone (`_open_beside`): no judge is called on a deletion, and
    in doubt Paris stays closed."""
    w = world
    ahead = _tomorrow()
    said = f"From {ahead.date().isoformat()} I left Paris."
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[said] = [Fact("u1", "left", "Paris", valid_from=ahead.date().isoformat())]
    w.write(OLD)
    w.stale = ("Paris",)
    new = w.write(said)
    w.stale = ()
    db.expire_all()
    (f,) = [x for x in _paris(db) if x.predicate_canonical == "lives_in"]
    assert f.invalid_at == ahead and f.invalidated_by_id is not None
    if rome:
        w.reads["I left Rome too."] = [Fact("u1", "left", "Rome")]
        w.write("I left Rome too.")
    _said_over(w)
    over = w.write(OVER)
    assert _delete(w, new, False)["facts_reopened"] == 0
    assert _delete(w, over, False)["facts_reopened"] == (0 if rome else 1)
    db.expire_all()
    lives = [x for x in db.query(AgentFact)
             if x.predicate_canonical == "lives_in" and x.invalid_at is None]
    assert len(lives) == (0 if rome else 1)


def test_a_closer_posted_by_hand_and_forgotten_by_hand_was_true(world, db):
    """A design choice (`given_back._retracted`): "I live in Paris", then
    Zanzibar posted by hand (POST /v1/facts), which closes Paris; "I used to
    live in Paris" lands on the closed row; the caller forgets Zanzibar by
    hand. Zanzibar has no memory, and its forget says it was true until
    then: what it closed stays history. Deleting the memory that said Paris
    was over gives nothing back, as deleting nothing would."""
    w = world
    used = "I used to live in Paris."
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[used] = [Fact("u1", "lives_in", "Paris", tense="past")]
    w.write(OLD)
    w.stale = ("Paris",)
    r = w.client.post("/v1/facts", json={"subject": "u1", "predicate": "lives_in",
                                         "object": "Zanzibar", "user_id": "u1"})
    w.stale = ()
    assert r.status_code == 201, r.text
    (f,) = _paris(db)
    (g,) = _zanzibar(db)
    assert f.invalidated_by_id == g.id and g.source_memory_id is None
    said = w.write(used)
    assert _enders(db, f) == {_uuid(said)}
    assert _forget(w, g.id)["status"] == "forgotten"
    assert _delete(w, said, False)["facts_reopened"] == 0
    assert "Paris" not in _open(db) and "Zanzibar" not in _open(db)


@pytest.mark.parametrize("mode", ["one_by_one", "waiting_together"])
def test_the_memory_that_said_it_was_over_edited_away_gives_it_back_from_the_edit(world, db,
                                                                                  monkeypatch,
                                                                                  mode):
    """"I live in Paris", "I no longer live in Paris", nothing else; the
    second edited into "I like cats" no longer says Paris is over, and Paris
    is true again from the edit's arrival: until then that memory said it
    was over (an edit is not an erasure)."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.write(OLD)
    _said_over(w)
    over = w.write(OVER)
    before = datetime.now(timezone.utc)
    _edit(w, monkeypatch, over, CATS, [Fact("u1", "likes", "cats")], mode)
    f, f2 = _paris(db)
    assert _enders(db, f) == set() and f2.invalid_at is None
    assert f2.valid_from >= before and f2.valid_from > f.invalid_at
    assert "Paris" in _open(db) and "cats" in _open(db)


# ── an edit of the memory that said it is over ─────────────────────────────

def test_the_memory_that_said_it_was_over_edited_still_saying_it(world, db, monkeypatch):
    """"I no longer live in Paris" edited into "I no longer live in Paris!"
    (a typo fixed): the new text's past statement lands on the closed row
    again, the memory still says Paris is over, and retracting Zanzibar's
    memory gives nothing back."""
    w = world
    new, (over,), ahead = _closed_ahead(w, monkeypatch=monkeypatch, then=_said_over(w))
    _edit(w, monkeypatch, over, "I no longer live in Paris!",
          [Fact("u1", "lives_in", "Paris", tense="past")])
    (f,) = _paris(db)
    assert _enders(db, f) == {_uuid(over)}
    assert _delete(w, new, False)["facts_reopened"] == 0
    assert "Paris" not in _open(db)


@pytest.mark.parametrize("mode", ["one_by_one", "waiting_together"])
def test_the_memory_that_said_it_was_over_edited_into_something_else(world, db, monkeypatch,
                                                                    mode):
    """Edited into "I like cats", it no longer says Paris is over: its word
    is taken back (its row of agent_fact_ended_by goes), Paris stays closed
    while Zanzibar from tomorrow stands, and retracting Zanzibar's memory
    gives it back from there, as in 1.0.13."""
    w = world
    new, (over,), ahead = _closed_ahead(w, monkeypatch=monkeypatch, then=_said_over(w))
    _edit(w, monkeypatch, over, CATS, [Fact("u1", "likes", "cats")], mode)
    (f,) = _paris(db)
    assert _enders(db, f) == set() and "Paris" not in _open(db)
    assert _delete(w, new, False)["facts_reopened"] == 1
    assert "Paris" in _open(db)


def test_a_past_statement_and_its_value_edited_away_gives_it_back(world, db, monkeypatch):
    """"I moved to Pro, I used to be on Basic" edited into "I like cats":
    the edit retires Pro and takes back the word that ended Basic in place,
    and Basic, still said by its own memory, is true again, as in 1.0.13."""
    w = world
    basic = "I am on the Basic plan."
    moved = "I moved to Pro, I used to be on Basic."
    w.reads[basic] = [Fact("u1", "has_plan", "Basic")]
    w.reads[moved] = [Fact("u1", "has_plan", "Basic", tense="past"),
                      Fact("u1", "has_plan", "Pro")]
    w.write(basic)
    m = w.write(moved)
    db.expire_all()
    (b,) = [f for f in db.query(AgentFact) if f.object_label == "Basic"]
    assert _enders(db, b) == {_uuid(m)}
    _edit(w, monkeypatch, m, CATS, [Fact("u1", "likes", "cats")])
    assert "Basic" in _open(db) and "Pro" not in _open(db)


# ── a merge moves the pointer to the closer's older twin ───────────────────

def _zanzibar_twins(w, *, separate_project, cats=False):
    """K ("Zanzibar Island", or "Zanzibar" in the default project) written
    first, beginning now; then Paris (January 2024), "I like cats" of 1
    March 2025 if `cats`, and G, "Zanzibar" of 1 March 2025, which closes
    Paris. Returns (K's memory, G's memory, the project G was written in or
    None)."""
    from korely_agent.services import projects as project_service

    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.reads[ISLAND] = [Fact("u1", "lives_in", "Zanzibar" if separate_project else "Zanzibar Island")]
    w.reads[CATS] = [Fact("u1", "likes", "cats")]
    project_service.get_or_create_default_project(w.db, w.tenant.id)
    k_mem = w.write(ISLAND)
    gamma = None
    if separate_project:
        gamma = project_service.create_project(w.db, tenant_id=w.tenant.id, name="Gamma")
        w.use(gamma.id)
    w.write(OLD, timestamp="2024-01-10T00:00:00Z")
    if cats:
        w.write(CATS, timestamp="2025-03-01T00:00:00Z")
    w.stale = ("Paris",)
    g_mem = w.write(NEW, timestamp="2025-03-01T00:00:00Z")
    w.stale = ()
    return k_mem, g_mem, gamma


def _merge(w, db, tenant, how, gamma):
    from korely_agent.services import projects as project_service
    from korely_agent.services.facts import entities

    if how == "project_deleted":
        project_service.delete_project(db, tenant.id, gamma)
        w.use()
    else:
        pid = project_service.get_or_create_default_project(db, tenant.id).id
        entities.declare_alias(db, tenant.id, pid, "u1", None, "Zanzibar Island", "Zanzibar")
        db.commit()
    db.expire_all()


@pytest.mark.parametrize("cats", [False, True], ids=["alone", "with_a_memory_of_that_date"])
@pytest.mark.parametrize("how", ["project_deleted", "alias"])
def test_a_merge_into_an_older_twin_that_begins_later_keeps_the_closing(world, db, tenant, how,
                                                                       cats):
    """The closer merges into its older twin, which begins after Paris
    ended (memory_core `store.merge_into` keeps the older row): a project
    deleted without its data folds its facts into the default project's
    (`projects._fold_duplicates`), an alias makes two names one
    (`store.recanonicalize`). The pointer moves to the twin; deleting every
    memory that said Zanzibar gives Paris back, also beside "I like cats" of
    the same date, which ended nothing (the third round of 9 Nov 2026 read
    the clocks and blamed it)."""
    w = world
    k_mem, g_mem, gamma = _zanzibar_twins(w, separate_project=how == "project_deleted", cats=cats)
    (f,) = _paris(db)
    g = next(z for z in _zanzibar(db) if z.id == f.invalidated_by_id)
    assert f.invalid_at == g.valid_from and _enders(db, f) == set()
    g_id = g.id      # G goes into its twin
    _merge(w, db, tenant, how, gamma)
    (f,) = _paris(db)
    (k,) = [x for x in db.query(AgentFact) if x.id == f.invalidated_by_id]
    assert k.id != g_id and k.valid_from > f.invalid_at and db.get(AgentFact, g_id) is None
    assert [_delete(w, m, False)["facts_reopened"] for m in (k_mem, g_mem)] == [0, 1]
    assert "Paris" in _open(db)


@pytest.mark.parametrize("how", ["alias", "project_deleted"])
def test_a_merge_after_a_past_statement_and_its_value_gives_it_back(world, db, tenant, how):
    """"I moved to Zanzibar, I used to live in Paris" closes Paris in place
    and points it at its own Zanzibar; that Zanzibar then merges into its
    older twin (an alias, a project deleted without its data). Deleting the
    twin's memory and that one leaves only "I live in Paris", and Paris is
    true again, as in 1.0.13."""
    from korely_agent.services import projects as project_service

    w = world
    moved = "I moved to Zanzibar, I used to live in Paris."
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[ISLAND] = [Fact("u1", "lives_in",
                            "Zanzibar" if how == "project_deleted" else "Zanzibar Island")]
    w.reads[moved] = [Fact("u1", "lives_in", "Paris", tense="past"),
                      Fact("u1", "lives_in", "Zanzibar")]
    project_service.get_or_create_default_project(db, tenant.id)
    k_mem = w.write(ISLAND)
    gamma = None
    if how == "project_deleted":
        gamma = project_service.create_project(db, tenant_id=tenant.id, name="Gamma")
        w.use(gamma.id)
    w.write(OLD, timestamp="2024-01-10T00:00:00Z")
    m = w.write(moved, timestamp="2025-03-01T00:00:00Z")
    db.expire_all()
    (f,) = _paris(db)
    g_id = f.invalidated_by_id
    assert f.tense == "past" and g_id is not None and _enders(db, f) == {_uuid(m)}
    _merge(w, db, tenant, how, gamma)
    (f,) = _paris(db)
    assert f.invalidated_by_id not in (None, g_id) and db.get(AgentFact, g_id) is None
    assert [_delete(w, x, False)["facts_reopened"] for x in (k_mem, m)] == [0, 1]
    assert "Paris" in _open(db)


# ── an edit of the fact's own memory ───────────────────────────────────────

@pytest.mark.parametrize("mode", ["one_by_one", "waiting_together"])
@pytest.mark.parametrize("erase", [False, True], ids=["forget", "erase"])
def test_an_edit_of_the_facts_own_memory_before_the_day_ahead_keeps_it_closed(world, db,
                                                                             monkeypatch, erase,
                                                                             mode):
    """"I live in Paris" edited into "I like cats" before the day Zanzibar
    begins: the edit ends Paris in place when the new text arrived (memory_core
    `store.retire_the_previous_version`), tense 'current', the judge's
    pointer left on the row, and records its memory as who ended it. No
    memory says Paris any more, and retracting Zanzibar's memory gives
    nothing back."""
    w = world
    new, _, ahead = _closed_ahead(w)
    (f,) = _paris(db)
    (g,) = _zanzibar(db)
    paris_memory = f.source_memory_id
    _edit(w, monkeypatch, mem_public_id(paris_memory), CATS, [Fact("u1", "likes", "cats")], mode)
    (f,) = _paris(db)
    assert f.tense == "current" and f.invalid_at < g.valid_from and f.invalidated_by_id == g.id
    assert _enders(db, f) == {paris_memory}
    assert _delete(w, new, erase)["facts_reopened"] == 0
    assert "Paris" not in _open(db) and len(_paris(db)) == 1
    assert not _paris_at(w, ahead + timedelta(days=1))


# ── memories of the same date that ended nothing ───────────────────────────

@pytest.mark.parametrize("when", ["before_the_closer", "after_the_closing"])
def test_a_memory_of_the_closers_date_ended_nothing(world, db, when):
    """A memory dated as Zanzibar's, which the judge closed Paris at, ended
    nothing: written before Zanzibar, or after the store learned Paris had
    ended (an import of one date). Forgetting Zanzibar's memory gives Paris
    back."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.reads[CATS] = [Fact("u1", "likes", "cats")]
    day = "2025-03-01T00:00:00Z"
    if when == "before_the_closer":
        w.write(CATS, timestamp=day)
    w.write(OLD, timestamp="2024-01-10T00:00:00Z")
    w.stale = ("Paris",)
    new = w.write(NEW, timestamp=day)
    w.stale = ()
    if when == "after_the_closing":
        w.write(CATS, timestamp=day)
    (f,) = _paris(db)
    assert f.invalid_at == datetime(2025, 3, 1, tzinfo=timezone.utc)
    assert _delete(w, new, False)["facts_reopened"] == 1
    assert "Paris" in _open(db)


def test_a_closing_learned_when_the_vector_came_beside_a_memory_of_that_date(world, db):
    """The judge's closing once the closer's vector came (facts/contradiction.py
    judges it then: Paris's end learned after "I like cats" of the same date
    arrived, as the rows of that judgement say it). The cats memory ended
    nothing, and forgetting Zanzibar's memory gives Paris back."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.reads[CATS] = [Fact("u1", "likes", "cats")]
    w.write(OLD, timestamp="2024-01-10T00:00:00Z")
    w.stale = ("Paris",)
    new = w.write(NEW, timestamp="2025-03-01T00:00:00Z")
    w.stale = ()
    w.write(CATS, timestamp="2025-03-01T00:00:00Z")
    (f,) = _paris(db)
    db.execute(text("UPDATE agent_facts SET closed_known_at = clock_timestamp() WHERE id = :f"),
               {"f": f.id})
    db.commit()
    assert _delete(w, new, False)["facts_reopened"] == 1
    assert "Paris" in _open(db)


def test_the_documented_order_beside_a_memory_of_that_date(world, db, monkeypatch):
    """"I like cats" of 1 November, written between Zanzibar and "I no
    longer live in Paris" of the same date: deleting the latter, then
    Zanzibar's, gives Paris back; the cats memory ended nothing."""
    w = world
    w.reads[CATS] = [Fact("u1", "likes", "cats")]
    _said_over(w)
    new, (cats, over), ahead = _closed_ahead(
        w, paris_said="2026-10-01T00:00:00Z", monkeypatch=monkeypatch,
        then=[(CATS, {"timestamp": "2026-11-01T00:00:00Z"}),
              (OVER, {"timestamp": "2026-11-01T00:00:00Z"})])
    assert [_delete(w, m, False)["facts_reopened"] for m in (over, new)] == [0, 1]
    assert "Paris" in _open(db)


@pytest.mark.parametrize("whose", ["another_end_user", "another_project"])
def test_a_memory_of_the_same_instant_elsewhere_is_not_the_one_that_ended_it(world, db, tenant,
                                                                            whose):
    """"I no longer live in Paris" of 1 November closed Paris in place, and
    a memory of another end user, or of another project, is dated 1
    November too and written meanwhile. Deleting the first memory, then
    Zanzibar's, leaves only "I live in Paris" in u1's memory, and Paris
    comes back: the other memory is no part of it."""
    from korely_agent.services import projects as project_service

    w = world
    new, _, ahead = _closed_ahead(w, paris_said="2026-10-01T00:00:00Z")
    elsewhere = "Rome is far away."
    w.reads[elsewhere] = [Fact("u2", "likes", "Rome")]
    if whose == "another_project":
        project_service.get_or_create_default_project(db, tenant.id)
        other = project_service.create_project(db, tenant_id=tenant.id, name="Other")
        w.use(other.id)
        w.write(elsewhere, timestamp="2026-11-01T00:00:00Z")
        w.use()
    else:
        w.write(elsewhere, user="u2", timestamp="2026-11-01T00:00:00Z")
    _said_over(w)
    over = w.write(OVER, timestamp="2026-11-01T00:00:00Z")
    (f,) = _paris(db)
    assert f.invalid_at == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert [_delete(w, m, False)["facts_reopened"] for m in (over, new)] == [0, 1]
    assert "Paris" in _open(db)


# ── the walls between accounts ──────────────────────────────────────────────

def _other_account(db):
    from korely_agent.db.models import Tenant

    t = Tenant(id=uuid.uuid4(), name="Another account")
    db.add(t)
    db.commit()
    return t


def test_a_record_of_another_account_does_not_keep_it_closed(world, db):
    """A forget record of another account naming this fact's id (a record
    can name any string) is not this account's word: retracting Zanzibar's
    memory gives Paris back from tomorrow."""
    w = world
    new, _, ahead = _closed_ahead(w)
    (f,) = _paris(db)
    other = _other_account(db)
    db.add(AgentForgetAudit(tenant_id=other.id, scope="fact", target_id=fact_public_id(f.id),
                            memories_forgotten=0, facts_invalidated=1))
    db.commit()
    assert _delete(w, new, False)["facts_reopened"] == 1


def test_a_memory_of_another_account_does_not_keep_it_closed(world, db, monkeypatch):
    """A row of agent_fact_ended_by that names a live memory of another
    account (no write of this account makes one) is not ended by this
    account's word: retracting Zanzibar's memory gives Paris back."""
    w = world
    new, (over,), ahead = _closed_ahead(w, monkeypatch=monkeypatch, then=_said_over(w))
    other = _other_account(db)
    theirs = AgentMemory(id=uuid.uuid4(), tenant_id=other.id, content="theirs", end_user_id="u1",
                         graph_sync_status="done")
    db.add(theirs)
    db.flush()
    (f,) = _paris(db)
    db.execute(text("UPDATE agent_fact_ended_by SET memory_id = :m WHERE fact_id = :f"),
               {"m": theirs.id, "f": f.id})
    db.commit()
    assert _delete(w, new, False)["facts_reopened"] == 1


# ── the rows closed before the record ───────────────────────────────────────

def test_a_row_ended_in_place_before_the_record_reads_as_until_1_0_13(world, db, monkeypatch):
    """A row a memory ended in place before 1.0.14 carries no
    row of agent_fact_ended_by (nothing wrote it, and nothing infers it from the
    clocks): retracting Zanzibar's memory gives Paris back from tomorrow, as
    until 1.0.13 (docs/guide/limits.md)."""
    w = world
    new, (over,), ahead = _closed_ahead(w, monkeypatch=monkeypatch, then=_said_over(w))
    db.execute(text("DELETE FROM agent_fact_ended_by"))
    db.commit()
    assert _delete(w, new, False)["facts_reopened"] == 1
    assert _paris_at(w, ahead + timedelta(days=1))


# ── the rest: two copies, twice, the fact's own memory, the person, the account

def test_two_copies_forgotten_by_hand_give_neither_back(world, db):
    """Paris told to agents a and b, closed in both by Zanzibar of agent z
    from tomorrow; the caller forgets both copies, and deleting agent z
    weighs the two in one deletion: neither comes back."""
    w = world
    ahead = _tomorrow()
    said = f"From {ahead.date().isoformat()} I live in Zanzibar."
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[said] = [Fact("u1", "lives_in", "Zanzibar", valid_from=ahead.date().isoformat())]
    w.write(OLD, agent="a")
    w.write(OLD, agent="b")
    w.stale = ("Paris",)
    w.write(said, agent="z")
    w.stale = ()
    copies = _paris(db)
    assert len(copies) == 2
    for f in copies:
        assert _forget(w, f.id)["status"] == "forgotten"
    assert _retract(w, None, "agent") == 0
    assert [x for x in _facts(w, as_of=(ahead + timedelta(days=1)).isoformat())
            if x["object"] == "Paris"] == []


def test_forgetting_it_twice_then_deleting_the_closer_twice_gives_nothing_back(world, db):
    """Two forgets of F (the second moves nothing), then Zanzibar's memory
    forgotten and erased after: nothing comes back at any step."""
    w = world
    new, _, ahead = _closed_ahead(w)
    (f,) = _paris(db)
    assert _forget(w, f.id)["status"] == "forgotten"
    assert _forget(w, f.id, _moment())["status"] == "already_forgotten"
    assert _delete(w, new, False)["facts_reopened"] == 0
    assert _delete(w, new, True)["facts_reopened"] == 0
    assert len(_paris(db)) == 1 and _open(db) == []
    assert not _paris_at(w, ahead + timedelta(days=1))
    assert orphans(db) == []


@pytest.mark.parametrize("erase", [False, True], ids=["forget", "erase"])
def test_deleting_the_forgotten_facts_own_memory_then_the_closers(world, db, erase):
    """F forgotten, then the memory that said Paris goes: nothing comes back
    and Zanzibar stays as it was, dated ahead; Zanzibar's memory after it
    gives nothing back either."""
    w = world
    new, _, ahead = _closed_ahead(w)
    (f,) = _paris(db)
    assert _forget(w, f.id)["status"] == "forgotten"
    assert _delete(w, mem_public_id(f.source_memory_id), erase)["facts_reopened"] == 0
    (g,) = _zanzibar(db)
    assert g.valid_from == ahead and g.invalid_at is None
    assert _delete(w, new, False)["facts_reopened"] == 0
    assert "Paris" not in _open(db) and len(_paris(db)) == (0 if erase else 1)
    assert orphans(db) == []


def test_erasing_the_end_user_after_the_forget_leaves_nothing(world, db):
    w = world
    _closed_ahead(w)
    (f,) = _paris(db)
    assert _forget(w, f.id)["status"] == "forgotten"
    r = w.client.delete("/v1/users/u1/memories")
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.query(AgentFact).count() == 0


@pytest.mark.cloud
def test_closing_the_account_after_the_forget_leaves_nothing(world, db, tenant):
    from korely_agent.hosted import closing

    w = world
    _closed_ahead(w)
    (f,) = _paris(db)
    assert _forget(w, f.id)["status"] == "forgotten"
    closing.close(db, tenant.id, via="api")
    db.expire_all()
    assert db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id).count() == 0


def test_forgetting_the_successor_by_hand(world, db):
    """G forgotten by hand: dated ahead, it ends where it begins and is true
    on no day (service.forget_fact). A forget of a fact gives nothing back,
    so F keeps the judge's end ahead (unchanged); retracting Zanzibar's
    memory then gives Paris back from there."""
    w = world
    new, _, ahead = _closed_ahead(w)
    (g,) = _zanzibar(db)
    assert _forget(w, g.id)["status"] == "forgotten"
    (f,) = _paris(db)
    (g,) = _zanzibar(db)
    assert g.invalid_at == g.valid_from == ahead and f.invalid_at == ahead
    assert _delete(w, new, False)["facts_reopened"] == 1
    f, f2 = _paris(db)
    assert f2.valid_from == ahead and f2.invalid_at is None


def test_forgetting_both_by_hand_gives_nothing_back(world, db):
    w = world
    new, _, ahead = _closed_ahead(w)
    (f,) = _paris(db)
    (g,) = _zanzibar(db)
    assert _forget(w, g.id)["status"] == "forgotten"
    assert _forget(w, f.id)["status"] == "forgotten"
    assert _delete(w, new, False)["facts_reopened"] == 0
    assert len(_paris(db)) == 1 and _open(db) == []
    assert not _paris_at(w, ahead + timedelta(days=1))


# ── a later value never moves an end already given (review of 9 Nov 2026) ─

def _post_ahead(w, obj, days):
    """POST /v1/facts: `obj` where u1 lives, from `days` days ahead, the
    judge closing what names Paris; its start."""
    begins = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0)
    w.stale = ("Paris",)
    try:
        r = w.client.post("/v1/facts", json={"subject": "u1", "predicate": "lives_in", "object": obj,
                                             "user_id": "u1", "valid_from": begins.isoformat()})
    finally:
        w.stale = ()
    assert r.status_code == 201, r.text
    return begins


@pytest.mark.parametrize("door", ["posted", "said"])
def test_a_value_that_begins_after_a_forget_ahead_leaves_the_forgets_end(world, db, door):
    """A review finding of 9 Nov 2026: Paris
    forgotten with an `at` ahead ends there; a value that begins later, and
    that the judge says contradicts Paris, moved that end to its own start
    (memory_core `judge.close` closed what is true now, and a fact forgotten
    on a day ahead is), so Paris read true again between the two. A value
    posted ten days ahead (the finding's case), and one a memory dates
    tomorrow after a forget of today. Now the end stays the caller's: Paris
    is not true after it, nothing names a closer, and deleting the value's
    memory gives nothing back."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.write(OLD)
    (f,) = _paris(db)
    if door == "posted":
        at = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    else:
        ahead = _tomorrow()
        now = datetime.now(timezone.utc)
        at = now + (ahead - now) / 2
    assert _forget(w, f.id, at)["status"] == "forgotten"
    if door == "posted":
        begins = _post_ahead(w, "Zanzibar", 10)
    else:
        said = f"From {ahead.date().isoformat()} I live in Zanzibar."
        w.reads[said] = [Fact("u1", "lives_in", "Zanzibar", valid_from=ahead.date().isoformat())]
        new = _say(w, None, "one_by_one", [(said, "u1", None, {})])[0]
        begins = ahead
    (g,) = _zanzibar(db)
    assert g.valid_from == begins and g.invalid_at is None
    (f,) = _paris(db)
    assert f.invalid_at == at and f.invalidated_by_id is None
    assert _enders(db, f) == set()
    assert not _paris_at(w, at + (begins - at) / 2)
    if door == "said":
        assert _delete(w, new, False)["facts_reopened"] == 0
        (f,) = _paris(db)
        assert f.invalid_at == at and "Paris" not in _open(db)
        assert not _paris_at(w, at + (begins - at) / 2)
    assert orphans(db) == []


def test_a_value_that_begins_after_the_end_the_judge_gave_leaves_it(world, db):
    """The same rule for an end ahead the judge gave: Paris closed tomorrow by
    "From <tomorrow> I live in Zanzibar", then Rome posted ten days ahead,
    which the judge says contradicts Paris, leaves Paris ending tomorrow
    under Zanzibar. Until 1.0.16 Rome moved the end to its own start, and
    Paris read true beside Zanzibar for nine days (Graphiti
    `resolve_edge_contradictions`: an edge that ended by the new edge's
    start is not invalidated)."""
    w = world
    _new, _, ahead = _closed_ahead(w)
    (g,) = _zanzibar(db)
    rome = _post_ahead(w, "Rome", 10)
    (f,) = _paris(db)
    assert f.invalid_at == ahead and f.invalidated_by_id == g.id
    assert not _paris_at(w, ahead + (rome - ahead) / 2)
