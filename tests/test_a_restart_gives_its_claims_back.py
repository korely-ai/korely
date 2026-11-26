"""A restart gives back the memories it was extracting (1.0.22).

The Cloud's deploy of 1.0.19 (13 Nov 2026): a memory written over MCP twenty
seconds before the restart stayed 'processing', and two memories of the same
person written after it waited with it, until the reaper's clock gave the
claim back fifteen minutes later (`drain_stale_minutes`). None was lost; the
person was held. Here a process holds a key the database keeps for as long
as the process is connected (services/holder.py): a claim whose key nobody
holds goes back to the queue at the next drain, one a live process holds is
left while its lock session is up, and a clean stop gives its claims back
itself. A process whose lock session alone was cut is read as ended until
its next drain takes its key again: meanwhile another process may give its
claims back, a whole person at a time, and its group stops at the first
memory that waits to be extracted again.

The process that dies is a real one: a child Python process that claims with
the drain's own code and is killed with SIGKILL, so its connections end as a
deploy's or a crash's do. No model is called.
"""
import itertools
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from korely_agent.db.models import AgentMemory
from korely_agent.services import holder as holders
from korely_agent.services.facts import drain as facts_drain
from korely_agent.services.facts import service as S
from korely_agent.services.memory_core import worker as mc_worker
from korely_agent.services.memory_core.fact_extractor import Extraction
from tests.thread_helpers import wait_idle

# The child: one drain tick with the drain's own code, its group held in its
# thread (as a group waits on a model), then it waits to be killed.
_CHILD = """
import threading, time
from korely_agent.db import get_sessionmaker
from korely_agent.services.facts import drain

never = threading.Event()

def held(gdb, tenant, project, end_user, rows, claims):
    never.wait()

drain._run_group = held
db = get_sessionmaker()()
out = drain.process_pending_memories(db, limit=10)
db.close()
print("claimed", out["claimed"], flush=True)
time.sleep(600)
"""


def _memory(db, tenant, end_user="u1", content="Maria moved to Milan.", minutes=0):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=end_user, content=content,
                    created_at=datetime.now(timezone.utc) + timedelta(minutes=minutes))
    db.add(m)
    db.commit()
    return m.id


def _row(db, mid):
    db.expire_all()
    return db.get(AgentMemory, mid)


def _a_process_that_claims(engine):
    """A child process that claimed the pending memories and holds them, and
    how many it claimed."""
    env = dict(os.environ, DATABASE_URL=engine.url.render_as_string(hide_password=False))
    child = subprocess.Popen([sys.executable, "-c", _CHILD], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
    lines = []
    reader = threading.Thread(target=lambda: lines.append(child.stdout.readline()), daemon=True)
    reader.start()
    reader.join(60)
    if not lines or not lines[0].startswith("claimed"):
        child.kill()
        child.wait()
        pytest.fail(f"the child process did not claim: {lines}")
    return child, int(lines[0].split()[1])


def _held(db, key) -> bool:
    """Whether a session of this database holds `key` now."""
    return bool(db.execute(text(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
        " AND objsubid = 2 AND classid = :space AND objid = :key"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"),
        {"space": holders.SPACE, "key": key}).scalar())


def _until_its_sessions_end(db, key, seconds=20.0):
    """The database ends a killed process's sessions when it sees their
    sockets close, a moment after the kill."""
    until = time.monotonic() + seconds
    while _held(db, key) and time.monotonic() < until:
        db.rollback()
        time.sleep(0.05)
    db.rollback()


@pytest.fixture
def worked(monkeypatch):
    """The memories this process's drain works, by id: the groups recorded,
    not extracted."""
    ids = []

    def record(gdb, tenant, project, end_user, rows, claims):
        ids.extend(m.id for m in rows)
        return len(rows), 0

    monkeypatch.setattr(facts_drain, "_run_group", record)
    return ids


def test_a_live_processs_claim_is_never_taken_and_an_ended_ones_is_taken_at_once(
        db, engine, tenant, worked):
    first = _memory(db, tenant)
    child, claimed = _a_process_that_claims(engine)
    try:
        assert claimed == 1
        row = _row(db, first)
        assert row.graph_sync_status == "processing" and row.graph_sync_claimed_by is not None
        key = row.graph_sync_claimed_by
        assert _held(db, key), "the child's key is not held while it runs"

        # While the child runs: its claim and its person stay its own, and
        # another person is not held by it.
        later = _memory(db, tenant, minutes=1)
        other = _memory(db, tenant, end_user="u2")
        out = facts_drain.process_pending_memories(db, limit=10)
        wait_idle(facts_drain._draining, 30)
        assert worked == [other], f"a live process's claim or person was taken: {out}"
        row = _row(db, first)
        assert (row.graph_sync_status, row.graph_sync_claimed_by) == ("processing", key)
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait()

    # The restart: the child is gone, and the next drain takes its claim and
    # the memory written after it at once, not after drain_stale_minutes.
    _until_its_sessions_end(db, key)
    out = facts_drain.process_pending_memories(db, limit=10)
    wait_idle(facts_drain._draining, 30)
    assert sorted(map(str, worked[1:])) == sorted(map(str, (first, later))), (
        f"an ended process's claim waited for the clock: {out}")


def test_an_unstamped_claim_waits_for_the_clock(db, tenant, worked):
    """A claim with no stamp (an inline write's, one made by a release before
    this one) cannot be told from a live one: it is left to the clock, as
    every claim was before, even with no key held anywhere."""
    mid = _memory(db, tenant)
    assert mc_worker.claim_this_one(db, _row(db, mid)) is not None
    facts_drain.process_pending_memories(db, limit=10)
    assert worked == []
    assert _row(db, mid).graph_sync_status == "processing"


def test_a_stamp_is_the_current_claims_only(db, tenant, worked):
    """The database clears the stamp whenever the claim changes: a stamp of a
    process that ended, left on a memory given back and claimed again by
    someone who does not stamp (an inline write, a release before this one),
    would give that live claim back."""
    mid = _memory(db, tenant)
    gone = 7  # a key no session holds
    db.query(AgentMemory).filter(AgentMemory.id == mid).update(
        {AgentMemory.graph_sync_status: "processing",
         AgentMemory.graph_sync_claimed_at: datetime.now(timezone.utc),
         AgentMemory.graph_sync_claimed_by: gone}, synchronize_session=False)
    db.commit()
    assert _row(db, mid).graph_sync_claimed_by == gone, "a stamp set with its claim was cleared"

    row = _row(db, mid)
    assert mc_worker.release(db, mc_worker.Claim(AgentMemory, mid, row.graph_sync_claimed_at,
                                                 row.content))
    assert _row(db, mid).graph_sync_claimed_by is None
    assert mc_worker.claim_this_one(db, _row(db, mid)) is not None
    facts_drain.process_pending_memories(db, limit=10)
    assert worked == [], "a live inline claim was given back as an ended process's"
    assert _row(db, mid).graph_sync_status == "processing"

    # An UPDATE of the stamp alone keeps it; a new claim time alone is a new
    # claim, and clears it.
    db.query(AgentMemory).filter(AgentMemory.id == mid).update(
        {AgentMemory.graph_sync_claimed_by: gone}, synchronize_session=False)
    db.commit()
    assert _row(db, mid).graph_sync_claimed_by == gone
    db.query(AgentMemory).filter(AgentMemory.id == mid).update(
        {AgentMemory.graph_sync_claimed_at: datetime.now(timezone.utc)},
        synchronize_session=False)
    db.commit()
    assert _row(db, mid).graph_sync_claimed_by is None


@pytest.fixture
def during_the_extraction(monkeypatch):
    """The extraction waits for `go`, and says when it has started; what it
    extracted is counted."""
    started, go, calls = threading.Event(), threading.Event(), []

    def extraction(content, **kw):
        calls.append(content)
        started.set()
        go.wait(30)
        return Extraction([], None)

    monkeypatch.setattr(S, "extract_facts_and_observation", extraction)
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
    yield started, go, calls
    go.set()
    # The group ends before the tables are emptied, also when the test failed
    # half way.
    wait_idle(facts_drain._draining, 30)


def _stamp(db, mid, key, status="processing"):
    db.query(AgentMemory).filter(AgentMemory.id == mid).update(
        {AgentMemory.graph_sync_status: status,
         AgentMemory.graph_sync_claimed_at: datetime.now(timezone.utc),
         AgentMemory.graph_sync_claimed_by: key}, synchronize_session=False)
    db.commit()


@pytest.fixture
def another_process(db):
    """A second live process's key, held for the test."""
    other = holders.Holder()
    with other.lock:
        key = other.key(db.get_bind())
    yield key
    other.give_back(lambda conn, k: 0)


def test_a_clean_stop_gives_its_claims_back_and_works_no_more(db, tenant,
                                                               during_the_extraction,
                                                               another_process):
    started, go, calls = during_the_extraction
    first = _memory(db, tenant, content="Maria moved to Milan.")
    second = _memory(db, tenant, content="Maria works at Acme.", minutes=1)
    # Another live process's claim, and a forgotten one of this process's.
    theirs = _memory(db, tenant, end_user="u2")
    _stamp(db, theirs, another_process)
    assert facts_drain.process_pending_memories(db, limit=10)["claimed"] == 2
    assert started.wait(30)
    mine = _row(db, first).graph_sync_claimed_by
    assert mine is not None and _row(db, second).graph_sync_claimed_by == mine
    forgotten = _memory(db, tenant, end_user="u3")
    _stamp(db, forgotten, mine)
    db.query(AgentMemory).filter(AgentMemory.id == forgotten).update(
        {AgentMemory.deleted_at: datetime.now(timezone.utc)}, synchronize_session=False)
    db.commit()

    assert facts_drain.give_back() == 2
    for m in (first, second):
        row = _row(db, m)
        assert (row.graph_sync_status, row.graph_sync_claimed_at, row.graph_sync_claimed_by) == (
            "pending", None, None)
    assert _row(db, theirs).graph_sync_claimed_by == another_process, "a live claim was given back"
    assert _row(db, forgotten).graph_sync_status != "pending", "a forgotten memory was queued"

    # Another process takes the second memory meanwhile. The memory in
    # flight ends: its write is discarded, and the group does not go on to
    # the next one, which is that process's now.
    assert mc_worker.claim_this_one(db, _row(db, second)) is not None
    go.set()
    assert wait_idle(facts_drain._draining, 30)
    assert calls == ["Maria moved to Milan."]
    assert _row(db, first).graph_sync_status == "pending"
    assert _row(db, second).graph_sync_status == "processing"
    # This process claims nothing more: what a tick of it claims now goes
    # straight back (`_stamped`).
    other = _memory(db, tenant, end_user="u4")
    assert facts_drain.process_pending_memories(db, limit=10)["claimed"] == 0
    assert _row(db, other).graph_sync_status == "pending"


def test_a_stamp_lands_only_on_the_claim_it_was_made_for(db, tenant):
    """The stamp follows the claim: rows whose claim was given back and taken
    again by someone else before the stamp keep that one's (none)."""
    mid = _memory(db, tenant)
    rows = mc_worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
    assert [m.id for m in rows] == [mid]
    row = _row(db, mid)
    assert mc_worker.release(db, mc_worker.Claim(AgentMemory, mid, row.graph_sync_claimed_at,
                                                 row.content))
    assert mc_worker.claim_this_one(db, _row(db, mid)) is not None
    facts_drain._stamped(db, rows)
    assert _row(db, mid).graph_sync_claimed_by is None


def test_only_this_lock_in_this_database_says_a_process_lives(db, tenant, worked):
    """A key held as another lock (the one-key form with the same bits,
    another space) or in another database of the same server says nothing
    about the process that stamped it: its claim goes back."""
    from sqlalchemy import create_engine

    from tests.conftest import server_url

    mid = _memory(db, tenant)
    key = 4242
    _stamp(db, mid, key)
    elsewhere = create_engine(server_url())
    here = db.get_bind().connect()
    here.detach()  # its locks end with it, never handed to the pool
    try:
        with elsewhere.connect() as c, here as h:
            assert c.execute(text("SELECT pg_try_advisory_lock(:s, :k)"),
                             {"s": holders.SPACE, "k": key}).scalar()
            assert h.execute(text("SELECT pg_try_advisory_lock(:b)"),
                             {"b": (holders.SPACE << 32) | key}).scalar()
            assert h.execute(text("SELECT pg_try_advisory_lock(:s, :k)"),
                             {"s": holders.SPACE + 1, "k": key}).scalar()
            facts_drain.process_pending_memories(db, limit=10)
            wait_idle(facts_drain._draining, 30)
            assert worked == [mid]
            h.execute(text("SELECT pg_advisory_unlock_all()"))
            c.execute(text("SELECT pg_advisory_unlock_all()"))
    finally:
        elsewhere.dispose()


def test_a_claim_made_while_the_process_stops_is_given_back(db, tenant):
    """The tick that claims while the stop gives back: what it claimed after
    the giving back goes back to the queue, not to the clock."""
    mid = _memory(db, tenant)
    facts_drain.give_back()
    assert facts_drain._claim(db, 10) == []
    row = _row(db, mid)
    assert (row.graph_sync_status, row.graph_sync_claimed_at) == ("pending", None)


def test_two_processes_never_hold_one_key(db, monkeypatch):
    """The second process draws the first one's key first: it is not free,
    so it takes the next one it draws."""
    a, b = holders.Holder(), holders.Holder()
    try:
        with a.lock:
            ka = a.key(db.get_bind())
        second = 1 if ka != 1 else 2
        draws = itertools.chain([ka - 1, second - 1], itertools.count(10))
        monkeypatch.setattr(holders.secrets, "randbelow", lambda n: next(draws))
        with b.lock:
            kb = b.key(db.get_bind())
        assert ka and kb == second
        assert _held(db, ka) and _held(db, kb)
        db.rollback()
        a.give_back(lambda conn, key: 0)
        assert not _held(db, ka) and _held(db, kb)
    finally:
        a.give_back(lambda conn, key: 0)
        b.give_back(lambda conn, key: 0)


def test_a_cut_connection_takes_its_key_again(db):
    """A holder whose connection was cut (the server restarted, the session
    ended by an administrator) takes the same key again at its next use, so
    the claims stamped with it stay its own."""
    h = holders.Holder()
    try:
        with h.lock:
            key = h.key(db.get_bind())
        pid = db.execute(text(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 2"
            " AND classid = :space AND objid = :key"), {"space": holders.SPACE, "key": key}).scalar()
        db.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        db.commit()
        _until_its_sessions_end(db, key)
        with h.lock:
            assert h.key(db.get_bind()) == key
        assert _held(db, key)
    finally:
        h.give_back(lambda conn, k: 0)
        db.rollback()


def test_a_failed_liveness_read_or_stamp_never_stops_the_drain(db, tenant, monkeypatch,
                                                               worked, caplog):
    """The clock gives back what cannot be told: a database that refuses
    the read of `pg_locks`, or the stamp, leaves the claim to the clock, and
    the memory is worked as before."""
    from sqlalchemy import literal_column

    first = _memory(db, tenant)
    monkeypatch.setattr(holders, "alive", lambda column: literal_column("no_such_column"))
    facts_drain.process_pending_memories(db, limit=10)
    wait_idle(facts_drain._draining, 30)
    assert worked == [first]

    second = _memory(db, tenant, end_user="u2")
    monkeypatch.setattr(facts_drain._holder, "key", lambda bind: 2**40)  # no integer holds it
    facts_drain.process_pending_memories(db, limit=10)
    wait_idle(facts_drain._draining, 30)
    assert worked == [first, second]
    assert _row(db, second).graph_sync_claimed_by is None
    assert "could not tell the claims" in caplog.text and "could not stamp" in caplog.text


def test_the_stop_of_the_app_gives_the_claims_back(db, monkeypatch):
    from fastapi.testclient import TestClient

    from korely_agent import worker
    from korely_agent.config import get_settings
    from korely_agent.main import create_app

    calls = []

    async def idle(stop):
        await stop.wait()

    monkeypatch.setenv("RUN_WORKER", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(worker, "run_forever", idle)
    monkeypatch.setattr(worker, "give_back", lambda: calls.append("given back"))
    with TestClient(create_app()):
        assert calls == []
    assert calls == ["given back"]


# ── The review of 13 Nov 2026 ────────────────────────────────────────────────

def _cut(db, key):
    """This process keeps running and its lock session ends (the database
    restarted, an administrator, `idle_session_timeout`)."""
    pid = db.execute(text(
        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 2"
        " AND classid = :space AND objid = :key"), {"space": holders.SPACE, "key": key}).scalar()
    db.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    db.commit()
    _until_its_sessions_end(db, key)


@pytest.fixture
def held_extraction(monkeypatch):
    """Each memory's text extracted, counted, and how many at once; the first
    extraction of `first` waits for `go`."""
    state = {"calls": [], "now": 0, "most": 0, "first": None}
    lock, go, started = threading.Lock(), threading.Event(), threading.Event()

    def extraction(content, **kw):
        with lock:
            state["calls"].append(content)
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
            first = content == state["first"] and state["calls"].count(content) == 1
        try:
            if first:
                started.set()
                go.wait(30)
            else:
                time.sleep(0.2)
        finally:
            with lock:
                state["now"] -= 1
        return Extraction([], None)

    monkeypatch.setattr(S, "extract_facts_and_observation", extraction)
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
    yield state, started, go
    go.set()
    wait_idle(facts_drain._draining, 60)


def test_a_cut_lock_session_never_gives_the_process_its_own_claims_back(db, tenant,
                                                                         held_extraction):
    """The process's next drain takes its key again before it gives back
    anything: its claims stay its own, and each memory is extracted once,
    one at a time. Before, it gave its own group back, claimed it again and
    ran a second group on the same person: six model calls for three
    memories, two of them at once."""
    state, started, go = held_extraction
    state["first"] = "M1"
    ids = [_memory(db, tenant, content=f"M{i}", minutes=i) for i in (1, 2, 3)]
    assert facts_drain.process_pending_memories(db, limit=10)["claimed"] == 3
    assert started.wait(30)
    key = _row(db, ids[0]).graph_sync_claimed_by
    _cut(db, key)

    again = facts_drain.process_pending_memories(db, limit=10)
    assert again["claimed"] == 0, again
    assert {_row(db, m).graph_sync_claimed_by for m in ids} == {key}
    assert _held(db, key), "the process did not take its own key again"
    go.set()
    assert wait_idle(facts_drain._draining, 60)
    assert sorted(state["calls"]) == ["M1", "M2", "M3"] and state["most"] == 1, state
    assert {_row(db, m).graph_sync_status for m in ids} == {"done"}


def test_a_group_stops_at_a_memory_another_process_claimed_since(db, tenant, held_extraction):
    """While this process's lock session is cut, another process's drain
    reads it as ended, gives its group back and claims it again. The group
    in flight ends its memory, whose write is discarded, and stops: it
    never extracts the next memory under the other process's claim. Before,
    it did, and the later memory was written while the earlier one was
    still being extracted."""
    state, started, go = held_extraction
    state["first"] = "M1"
    first = _memory(db, tenant, content="M1")
    second = _memory(db, tenant, content="M2", minutes=1)
    assert facts_drain.process_pending_memories(db, limit=10)["claimed"] == 2
    assert started.wait(30)
    _cut(db, _row(db, first).graph_sync_claimed_by)

    # The other process: its give-back, then its claim (no stamp of its own).
    assert facts_drain._the_ended_ones_back(db, 10) == 2
    taken = mc_worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
    assert {m.id for m in taken} == {first, second}
    theirs = _row(db, second).graph_sync_claimed_at
    go.set()
    assert wait_idle(facts_drain._draining, 60)
    assert state["calls"] == ["M1"], state
    for m in (first, second):
        row = _row(db, m)
        assert (row.graph_sync_status, row.graph_sync_claimed_at) == ("processing", theirs), (
            "the other process's claim was worked, filed or given back")


def test_a_group_whose_process_gave_back_puts_its_rest_back(db, tenant, held_extraction,
                                                             monkeypatch):
    """At the stop the group in flight ends its memory and gives the rest of
    its claim back to the queue, unstamped ones included (the stamp failed),
    instead of leaving them 'processing' for the clock."""
    state, started, go = held_extraction
    state["first"] = "M1"
    monkeypatch.setattr(facts_drain._holder, "key", lambda bind: None)   # nothing stamped
    _memory(db, tenant, content="M1")
    second = _memory(db, tenant, content="M2", minutes=1)
    assert facts_drain.process_pending_memories(db, limit=10)["claimed"] == 2
    assert started.wait(30)
    assert facts_drain.give_back() == 0
    go.set()
    assert wait_idle(facts_drain._draining, 60)
    assert state["calls"] == ["M1"]
    row = _row(db, second)
    assert (row.graph_sync_status, row.graph_sync_claimed_at) == ("pending", None)


def _waiting_on_a_row_lock(db) -> bool:
    """A session of this database waits on a lock, within ten seconds."""
    until = time.monotonic() + 10
    while time.monotonic() < until:
        db.rollback()
        if db.execute(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                           " AND datname = current_database()")).scalar():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("theirs", [99, None], ids=["stamped", "not-yet-stamped"])
def test_a_give_back_leaves_a_claim_taken_again_while_it_waited(db, tenant, engine, theirs):
    """The give-back of an ended process's claim waits on the row lock of a
    live process that is giving that claim back, claiming it again and
    stamping it with its own held key (or not yet: the instant between a
    claim and its stamp). Once that commits, the claim is the live
    process's, and stays so."""
    from sqlalchemy.orm import Session

    mid = _memory(db, tenant)
    _stamp(db, mid, 7)   # a key no session holds
    live, b = engine.connect(), engine.connect()
    live.detach()
    b.detach()
    c_session = Session(bind=engine)
    t = None
    try:
        assert live.execute(text("SELECT pg_try_advisory_lock(:s, 99)"),
                            {"s": holders.SPACE}).scalar()
        live.commit()
        b.execute(text("UPDATE agent_memories SET graph_sync_status = 'pending',"
                       " graph_sync_claimed_at = NULL WHERE id = :id"), {"id": mid})
        b.execute(text("UPDATE agent_memories SET graph_sync_status = 'processing',"
                       " graph_sync_claimed_at = now() WHERE id = :id"), {"id": mid})
        if theirs:
            b.execute(text("UPDATE agent_memories SET graph_sync_claimed_by = :k WHERE id = :id"),
                      {"k": theirs, "id": mid})
        result = {}
        t = threading.Thread(target=lambda: result.setdefault(
            "n", facts_drain._the_ended_ones_back(c_session, 10)))
        t.start()
        # Since 1.0.40 it skips the row the live process holds (SKIP LOCKED:
        # an account's erasure holds its rows to its commit, and this claim
        # is every account's), so it ends without waiting and leaves it.
        t.join(30)
        assert not t.is_alive(), "the give-back waited on the live process's row lock"
        b.commit()
        row = _row(db, mid)
        assert result == {"n": 0}
        assert (row.graph_sync_status, row.graph_sync_claimed_by) == ("processing", theirs)
    finally:
        # The holder first: a give-back that waits (a regression) is let go
        # and ends, so the test fails instead of hanging.
        b.close()
        if t is not None:
            t.join(30)
        c_session.close()
        live.execute(text("SELECT pg_advisory_unlock_all()"))
        live.close()


def test_a_give_back_leaves_a_claim_whose_key_came_back_meanwhile(db, tenant):
    """Between the read of the ended claims and their give-back, the process
    takes its key again (its lock session was only cut): the give-back
    reads the key's liveness again, and leaves the claim."""
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session

    mid = _memory(db, tenant)
    _stamp(db, mid, 4243)
    back = create_engine(db.get_bind().url)
    lock = db.get_bind().connect()
    lock.detach()

    @event.listens_for(back, "before_cursor_execute")
    def the_key_comes_back(conn, cursor, statement, *a):
        if statement.lstrip().startswith("UPDATE agent_memories"):
            lock.execute(text("SELECT pg_try_advisory_lock(:s, 4243)"), {"s": holders.SPACE})
            lock.commit()

    session = Session(bind=back)
    try:
        assert facts_drain._the_ended_ones_back(session, 10) == 0
        row = _row(db, mid)
        assert (row.graph_sync_status, row.graph_sync_claimed_by) == ("processing", 4243)
    finally:
        session.close()
        back.dispose()
        lock.execute(text("SELECT pg_advisory_unlock_all()"))
        lock.close()


def test_a_give_back_leaves_a_claim_whose_key_came_back_while_it_waited(db, tenant, engine):
    """The give-back waits on a row lock a write that changes neither stamp
    nor claim time holds, and meanwhile the process takes its key again: the
    claim is left. Before, the statement that waited read `pg_locks` as it
    was before the wait and gave the live claim back (probe p2b of the
    review of 13 Nov 2026): one more model call."""
    from sqlalchemy.orm import Session

    mid = _memory(db, tenant)
    _stamp(db, mid, 4242)   # a key no session holds
    other, back = engine.connect(), engine.connect()
    other.detach()
    back.detach()
    c_session = Session(bind=engine)
    t = None
    try:
        other.execute(text("UPDATE agent_memories SET embedding_attempts ="
                           " embedding_attempts + 1 WHERE id = :id"), {"id": mid})
        result = {}
        t = threading.Thread(target=lambda: result.setdefault(
            "n", facts_drain._the_ended_ones_back(c_session, 10)))
        t.start()
        # Since 1.0.40 the held row is skipped (SKIP LOCKED), not waited on.
        t.join(30)
        assert not t.is_alive(), "the give-back waited on the row lock"
        assert back.execute(text("SELECT pg_try_advisory_lock(:s, 4242)"),
                            {"s": holders.SPACE}).scalar()
        back.commit()
        other.commit()
        row = _row(db, mid)
        assert result == {"n": 0}
        assert (row.graph_sync_status, row.graph_sync_claimed_by) == ("processing", 4242)
    finally:
        # The holder first, as above: a waiting give-back ends, never hangs.
        other.close()
        if t is not None:
            t.join(30)
        c_session.close()
        back.execute(text("SELECT pg_advisory_unlock_all()"))
        back.close()


def test_a_give_back_takes_whole_persons(db, tenant):
    """A process ended holding two persons' memories: a give-back of at most
    one gives back all of one person's, the other's at the next."""
    first = [_memory(db, tenant, end_user="u1", content=f"M{i}", minutes=i) for i in (1, 2, 3)]
    second = [_memory(db, tenant, end_user="u2", content=f"N{i}", minutes=i) for i in (1, 2)]
    for m in first + second:
        _stamp(db, m, 4244)   # a key no session holds
    assert facts_drain._the_ended_ones_back(db, 1) == 3
    assert [_row(db, m).graph_sync_status for m in first] == ["pending"] * 3
    assert [_row(db, m).graph_sync_status for m in second] == ["processing"] * 2
    assert facts_drain._the_ended_ones_back(db, 1) == 2


def _three_of_one_person_in_flight(db, tenant, held_extraction):
    """M1, M2 and M3 of one person claimed by this process's drain, M1
    being extracted (it waits for `go`)."""
    state, started, go = held_extraction
    state["first"] = "M1"
    ids = [_memory(db, tenant, content=f"M{i}", minutes=i) for i in (1, 2, 3)]
    assert facts_drain.process_pending_memories(db, limit=10)["claimed"] == 3
    assert started.wait(30)
    return ids


def test_a_cut_process_loses_its_whole_person_and_writes_nothing_out_of_order(
        db, tenant, held_extraction):
    """The process's lock session is cut while it extracts M1; another
    process gives back at most one person's worth, then the process takes
    its key again and claims the person anew. Before, the give-back took any
    part of the person (a bare LIMIT), the group skipped the memory given
    back and wrote the later one: M1, M3, then M2 (probe p7)."""
    from sqlalchemy.orm import Session

    state, _started, go = held_extraction
    ids = _three_of_one_person_in_flight(db, tenant, held_extraction)
    _cut(db, _row(db, ids[0]).graph_sync_claimed_by)
    other = Session(bind=db.get_bind())
    try:
        assert facts_drain._the_ended_ones_back(other, 1) == 3
    finally:
        other.close()
    facts_drain.process_pending_memories(db, limit=10)
    go.set()
    assert wait_idle(facts_drain._draining, 60)
    assert state["calls"] == ["M1", "M1", "M2", "M3"], state
    assert [_row(db, m).graph_sync_status for m in ids] == ["done"] * 3


def test_a_group_stops_at_a_memory_back_in_the_queue(db, tenant, held_extraction):
    """M2 goes back to the queue while the group extracts M1 (a give-back
    of part of the person, an edit): the group writes M1, stops and gives M3
    back, and the next drain extracts M2 before M3. Before, the group skipped
    M2 and wrote M3 (probe p7b)."""
    state, _started, go = held_extraction
    ids = _three_of_one_person_in_flight(db, tenant, held_extraction)
    _cut(db, _row(db, ids[0]).graph_sync_claimed_by)
    stamp = AgentMemory.graph_sync_claimed_by
    assert db.execute(facts_drain._back_to_the_queue(
        AgentMemory.id == ids[1], ~holders.alive(stamp))).rowcount == 1
    db.commit()
    assert facts_drain.process_pending_memories(db, limit=10)["claimed"] == 0
    go.set()
    assert wait_idle(facts_drain._draining, 60)
    assert [_row(db, m).graph_sync_status for m in ids] == ["done", "pending", "pending"]
    facts_drain.process_pending_memories(db, limit=10)
    assert wait_idle(facts_drain._draining, 60)
    assert state["calls"] == ["M1", "M2", "M3"], state
    assert [_row(db, m).graph_sync_status for m in ids] == ["done"] * 3


def test_a_group_stops_when_the_memory_it_extracted_went_back(db, tenant, held_extraction):
    """M1 is edited while the group extracts it: its write is discarded and
    it waits again, so the group stops before M2 and gives M2 and M3 back,
    and the next drain extracts the three in their order."""
    state, _started, go = held_extraction
    ids = _three_of_one_person_in_flight(db, tenant, held_extraction)
    db.query(AgentMemory).filter(AgentMemory.id == ids[0]).update(
        {AgentMemory.graph_sync_status: "pending", AgentMemory.graph_sync_claimed_at: None},
        synchronize_session=False)   # what an edit does (memory_engine)
    db.commit()
    go.set()
    assert wait_idle(facts_drain._draining, 60)
    assert state["calls"] == ["M1"], state
    assert [_row(db, m).graph_sync_status for m in ids] == ["pending"] * 3
    facts_drain.process_pending_memories(db, limit=10)
    assert wait_idle(facts_drain._draining, 60)
    assert state["calls"] == ["M1", "M1", "M2", "M3"], state


def test_a_failed_group_files_only_its_own_claim(db, tenant):
    """A group that raises files as errors the rows still its tick's claim,
    never one another process claimed since."""
    from datetime import timedelta

    mine, theirs = _memory(db, tenant), _memory(db, tenant, minutes=1)
    rows = mc_worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
    claims = facts_drain._claims_of(rows)
    db.query(AgentMemory).filter(AgentMemory.id == theirs).update(
        {AgentMemory.graph_sync_claimed_at: claims[theirs] + timedelta(seconds=1)},
        synchronize_session=False)
    db.commit()
    facts_drain._file_as_errors(db, claims)
    assert _row(db, mine).graph_sync_status == "error"
    assert _row(db, theirs).graph_sync_status == "processing"


def test_a_key_is_returned_only_while_its_session_holds_it(db, tenant, monkeypatch, worked):
    """Taking a key again fails (the pool full, the database refusing): the
    holder answers no key, and the claims wait for the clock, never stamped
    with a key no session holds; a process with no key gives back nothing,
    its own claims included; the next take tries the old key first."""
    first = _memory(db, tenant)
    facts_drain.process_pending_memories(db, limit=10)
    wait_idle(facts_drain._draining, 30)
    key = _row(db, first).graph_sync_claimed_by
    assert key is not None and _held(db, key)
    _cut(db, key)
    real_take = holders.Holder._take

    def refused(self):
        raise TimeoutError("QueuePool limit reached (stand-in)")

    monkeypatch.setattr(holders.Holder, "_take", refused)
    mid = _memory(db, tenant, end_user="u2")
    facts_drain.process_pending_memories(db, limit=10)
    wait_idle(facts_drain._draining, 30)
    assert worked == [first, mid]
    assert _row(db, mid).graph_sync_claimed_by is None, "stamped with a key nobody holds"
    row = _row(db, first)
    assert (row.graph_sync_status, row.graph_sync_claimed_by) == ("processing", key), (
        "a process with no key gave its own claim back")
    monkeypatch.setattr(holders.Holder, "_take", real_take)
    with facts_drain._holder.lock:
        assert facts_drain._holder.key(db.get_bind()) == key
    assert _held(db, key)


def test_the_key_is_taken_on_a_connection_of_its_own(db):
    """The tick that asks holds a connection of its pool: the holder never
    asks that pool for a second one (the nested checkout of the audit of 27
    Oct 2026). Here the asker's pool has no connection left to give."""
    from sqlalchemy import create_engine

    full = create_engine(db.get_bind().url, pool_size=1, max_overflow=0, pool_timeout=0.5)
    h = holders.Holder()
    try:
        with full.connect():
            with h.lock:
                key = h.key(full)
        assert key is not None and _held(db, key)
    finally:
        h.give_back(lambda conn, k: 0)
        full.dispose()
        db.rollback()


def _holders_state(db, key):
    db.rollback()
    return db.execute(text(
        "SELECT a.state FROM pg_stat_activity a JOIN pg_locks l ON l.pid = a.pid"
        " WHERE l.locktype = 'advisory' AND l.objsubid = 2 AND l.classid = :space"
        " AND l.objid = :key"), {"space": holders.SPACE, "key": key}).scalar()


def test_the_holders_session_never_idles_inside_a_transaction(db):
    """Its session lives as long as the process: a transaction left open
    would hold back vacuum for all that time. Both its statements, the take
    and the check that it still holds its key, are committed."""
    h = holders.Holder()
    try:
        with h.lock:
            key = h.key(db.get_bind())
        assert _holders_state(db, key) == "idle"
        with h.lock:
            assert h.key(db.get_bind()) == key
        assert _holders_state(db, key) == "idle"
    finally:
        h.give_back(lambda conn, k: 0)
        db.rollback()


def test_a_session_that_lost_its_key_takes_it_again(db):
    """The connection answers but its session no longer holds the key (what
    a pooler in transaction mode does): the holder sees it on its own
    session, `pg_backend_pid`, and takes the key again."""
    h = holders.Holder()
    try:
        with h.lock:
            key = h.key(db.get_bind())
            h._conn.execute(text("SELECT pg_advisory_unlock_all()"))
            h._conn.commit()
        assert not _held(db, key)
        db.rollback()
        with h.lock:
            assert h.key(db.get_bind()) == key
        assert _held(db, key)
    finally:
        h.give_back(lambda conn, k: 0)
        db.rollback()


def test_a_stamp_and_the_giving_back_never_cross(db, tenant):
    """The stop gives back while a tick is stamping its claim: it waits for
    the stamp, then gives that claim back too. Without the holder's lock on
    either side, the stop gave back nothing yet stamped and the claim stayed
    'processing', stamped with a key its process was letting go."""
    mid = _memory(db, tenant)
    rows = mc_worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
    holder = facts_drain._holder
    real_lock, real_key = holder.lock, holder.key
    asked, done = threading.Event(), {}
    stop = threading.Thread(target=lambda: done.setdefault("n", facts_drain.give_back()))

    class Watched:
        """The holder's lock, saying when the stop asks for it."""
        def __enter__(self):
            if threading.current_thread() is stop:
                asked.set()
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

    def slow_key(bind):
        asked.wait(3)   # the stop has asked for the lock (or never will)
        return real_key(bind)

    holder.lock = Watched()
    holder.key = slow_key
    try:
        threading.Timer(0.2, stop.start).start()
        facts_drain._stamped(db, rows)
        stop.join(30)
    finally:
        holder.lock, holder.key = real_lock, real_key
    row = _row(db, mid)
    assert done == {"n": 1} and (row.graph_sync_status, row.graph_sync_claimed_by) == (
        "pending", None)


def test_the_worker_on_its_own_gives_its_claims_back_at_its_stop(monkeypatch):
    """`python -m korely_agent.worker` (RUN_WORKER=false beside the API) gives
    its claims back when its loop ends, as the API's stop does."""
    from korely_agent import worker
    from korely_agent.core import llm
    from korely_agent.services import vectors

    calls = []

    async def ends_at_once(stop):
        calls.append("ran")

    monkeypatch.setattr(worker, "run_forever", ends_at_once)
    monkeypatch.setattr(worker, "give_back", lambda: calls.append("given back"))
    monkeypatch.setattr(llm, "pick_distance_thresholds", lambda: None)
    monkeypatch.setattr(vectors, "refuse_vectors_of_another_model", lambda: None)
    assert worker.main() == 0
    assert calls == ["ran", "given back"]


_STREAMING_APP = '''
import asyncio
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

@asynccontextmanager
async def lifespan(app):
    yield
    print("the shutdown ran", flush=True)

async def stream(request):
    async def never_ends():
        while True:
            yield b"data: ping\\n\\n"
            await asyncio.sleep(0.2)
    return StreamingResponse(never_ends(), media_type="text/event-stream")

app = Starlette(routes=[Route("/stream", stream)], lifespan=lifespan)
'''


def test_the_api_runs_its_shutdown_before_dockers_stop_timeout(tmp_path):
    """The app's shutdown is where the worker gives its claims back. uvicorn
    runs it only once the open connections have ended, and an MCP client's
    stream never ends: with the image's flags, a server holding one open
    stream runs its shutdown and exits within Docker's stop timeout (10 s,
    `docker stop` and compose's `stop_grace_period`, which the shipped
    compose does not change). Before the flag it was still waiting when
    Docker killed it (the review of 13 Nov 2026)."""
    import http.client
    import json
    import re
    import socket
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert "stop_grace_period" not in (root / "docker-compose.yml").read_text(), (
        "the compose file sets its own stop timeout: compare with that one")
    cmd = re.search(r"^CMD (\[.*?\])", (root / "Dockerfile").read_text(), re.S | re.M)
    argv = json.loads(cmd.group(1).replace("\\\n", ""))
    flags = argv[argv.index("korely_agent.main:app") + 1:]
    (tmp_path / "streaming.py").write_text(_STREAMING_APP)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "streaming:app", "--app-dir", str(tmp_path), *flags,
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = []
    threading.Thread(target=lambda: out.extend(server.stdout), daemon=True).start()
    try:
        until = time.monotonic() + 30
        while True:
            try:
                client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                client.request("GET", "/stream")
                break
            except OSError:
                assert time.monotonic() < until, f"the server did not start: {out}"
                time.sleep(0.1)
        response = client.getresponse()
        assert response.read1(16).startswith(b"data")
        stopped = time.monotonic()
        server.send_signal(signal.SIGTERM)
        server.wait(10)
        took = time.monotonic() - stopped
    finally:
        server.kill()
        server.wait()
    time.sleep(0.2)
    assert any("the shutdown ran" in line for line in out), (took, out)
