"""The windows of long memories (1.0.33, MEMORY_WINDOWS) under the cases a
second reading found (17 Nov 2026), each a test that failed on the first
version of the branch or let a change of the rule pass unseen:

- the backfill against an edit, a forget and another backfill of the same
  memory, in two real sessions: an edit never fails because of it, a
  forgotten memory keeps no window, windows are never made of an old text,
  never made twice (services/memory_windows.fill_batch: each memory held FOR
  NO KEY UPDATE SKIP LOCKED, its text and its forget read under the lock);
- the setting off with windows stored: no search reads them, no worker
  computes them;
- the worker's job never runs back to back on a backlog (no `more`);
- the max of the two scores, never the window's in place of the memory's
  own (Dai and Callan, SIGIR 2019, section 3, MaxP);
- every wall of a search on the windows, and the windows of other accounts
  nearer than an account's own;
- the edges of the windows' offsets in mixed scripts.
"""
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from korely_agent.db.models import AgentMemory, AgentMemoryWindow, Tenant
from korely_agent.services import memory_windows, vectors
from korely_agent.services.memory_engine import AgentMemoryEngine
from korely_agent.services.sentinels import EVERY_PROJECT
from tests.conftest import seeded_project_id
from tests.test_memory_windows_go_with_the_words import (
    LONG, Q, QUESTION, SHORT, Table, drained, engine, rows, search, setting, unit, windows)


@pytest.fixture
def embedder(monkeypatch):
    """The embedder of test_memory_windows_go_with_the_words: a vector per
    text, the others far."""
    e = Table()
    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
    for p in ("korely_agent.core.llm.embed", "korely_agent.services.memory_engine.embed",
              "korely_agent.services.context.embed"):
        monkeypatch.setattr(p, e)
    return e


@pytest.fixture
def on(monkeypatch):
    setting(monkeypatch, True)

EDITED_SHORT = "The horse stayed home. Nothing else to say about it."


#: Every lock wait of these tests is bounded: a lock that never comes (a
#: change that drops the backfill's row lock or its SKIP LOCKED makes one
#: session wait for another of the same thread) fails the test, never hangs
#: it with a session open that keeps the database's cleanup waiting.
WAIT = "3s"
THREAD_WAIT = "20s"


def _bounded(s, wait=WAIT):
    """`s` with its current transaction's lock waits bounded (SET LOCAL: a
    pooled connection keeps no setting of a test)."""
    s.execute(text(f"SET LOCAL lock_timeout = '{wait}'"))
    return s


@contextmanager
def _other(db):
    """A second session on the same database (another request, another
    process), rolled back and closed whatever the test did."""
    s = Session(bind=db.get_bind())
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def _held_batch(s):
    """The first steps of `fill_batch` in session `s`, its transaction left
    open: the memories held, their windows made, not committed."""
    _bounded(s)
    rows, _last = memory_windows._locked_batch(s, after=None, batch=200)
    for r in rows:
        memory_windows.make(s, r)
    return rows


def _in_a_thread(db, work):
    """`work(session)` in a thread, on a session of its own whose lock waits
    are bounded by THREAD_WAIT: (thread, its result or error)."""
    out = {}

    def run():
        with _other(db) as s:
            try:
                _bounded(s, THREAD_WAIT)
                out["r"] = work(s)
            except Exception as ex:  # noqa: BLE001 - the test reads it
                out["e"] = f"{type(ex).__name__}: {str(ex).splitlines()[0][:200]}"

    t = threading.Thread(target=run)
    t.start()
    return t, out


# ── the backfill and the other writers of one memory ────────────────────────

def test_an_edit_waits_for_the_backfill_and_wins(db, tenant, embedder, monkeypatch):
    """Setting on, a memory of before being given its windows, and a PATCH
    of it at the same time: the PATCH waits for the backfill's commit, then
    replaces its windows with those of the new text. It answered 500
    (UniqueViolation) and the edit was lost."""
    setting(monkeypatch, False)
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    setting(monkeypatch, True)
    edited = LONG.replace("zebra", "horse")
    with _other(db) as a:
        assert _held_batch(a)
        t, out = _in_a_thread(db, lambda s: engine(s, tenant).update(str(m.id), edited))
        time.sleep(1.0)
        alive = t.is_alive()
        a.commit()
    t.join(30)
    assert alive, "the edit did not wait for the memory the backfill holds"
    assert "e" not in out and out.get("r"), out
    db.expire_all()
    assert db.get(AgentMemory, m.id).content == edited
    assert [(w.start_char, w.end_char) for w in windows(db, m.id)] == \
        memory_windows.windows_of(edited)


def test_a_memory_an_edit_holds_is_left_to_the_edit(db, tenant, embedder, monkeypatch):
    """The other order: the edit holds the row, the backfill skips it."""
    setting(monkeypatch, False)
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    other = engine(db, tenant).add(LONG + " Another one.", end_user_id="maria")
    setting(monkeypatch, True)
    with _other(db) as e:
        e.query(AgentMemory).filter(AgentMemory.id == m.id).update(
            {"updated_at": AgentMemory.updated_at}, synchronize_session=False)  # the edit's lock
        out = memory_windows.fill_batch(_bounded(db))
    assert out["read"] == 1 and windows(db, m.id) == [] and windows(db, other.id)


def test_two_backfills_never_make_one_memorys_windows_twice(db, tenant, embedder, monkeypatch):
    setting(monkeypatch, False)
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    setting(monkeypatch, True)
    with _other(db) as a, _other(db) as b:
        assert _held_batch(a)
        assert memory_windows.fill_batch(_bounded(b))["read"] == 0     # held: skipped
        a.commit()
        assert memory_windows.fill_batch(_bounded(b))["windows"] == 0  # read again
    assert [(w.start_char, w.end_char) for w in windows(db, m.id)] == \
        memory_windows.windows_of(LONG)


def test_windows_committed_after_the_batch_was_read_are_read_again(db, tenant, embedder,
                                                                   monkeypatch):
    """The batch is one statement: a writer can commit a memory's windows
    after the statement's snapshot and before its lock reaches the row, so
    the batch reads the windows again, in a statement of its own, and makes
    none for that memory (a UniqueViolation without the second reading)."""
    setting(monkeypatch, False)
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    setting(monkeypatch, True)
    real = memory_windows._locked_batch

    def late_writer(s, **kw):
        found = real(s, **kw)
        with _other(db) as c:
            _bounded(c)
            memory_windows.make(c, c.get(AgentMemory, m.id))
            c.commit()
        return found

    monkeypatch.setattr(memory_windows, "_locked_batch", late_writer)
    out = memory_windows.fill_batch(_bounded(db))
    assert out["read"] == 1 and out["windows"] == 0
    assert [(w.start_char, w.end_char) for w in windows(db, m.id)] == \
        memory_windows.windows_of(LONG)


@pytest.mark.parametrize("order", ["edit-first", "backfill-first"])
def test_while_off_the_windows_are_those_of_the_text_under_the_lock(db, tenant, embedder,
                                                                   monkeypatch, order):
    """`--while-off` and an edit to a short text: never windows of the old
    text's offsets on the new one."""
    setting(monkeypatch, False)
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    if order == "edit-first":
        engine(db, tenant).update(str(m.id), EDITED_SHORT)
        memory_windows.fill(_bounded(db))
    else:
        with _other(db) as a:
            assert _held_batch(a)
            t, out = _in_a_thread(db, lambda s: engine(s, tenant).update(str(m.id), EDITED_SHORT))
            time.sleep(1.0)
            a.commit()
        t.join(30)
        assert "e" not in out, out
    db.expire_all()
    assert db.get(AgentMemory, m.id).content == EDITED_SHORT
    assert windows(db, m.id) == []


@pytest.mark.parametrize("order", ["forget-first", "backfill-first"])
def test_a_forget_and_the_backfill_leave_no_window(db, tenant, embedder, monkeypatch, order):
    """A forget at the same time as the backfill: the forgotten memory keeps
    no window, none waits for a vector for ever, and the command's next run
    ends well. It kept two rows, `pending`, and every run exited 1."""
    from korely_agent import manage
    from korely_agent.services.facts import service as facts

    setting(monkeypatch, False)
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    setting(monkeypatch, True)
    if order == "forget-first":
        facts.forget_memory(db, tenant.id, m.id, project_id=EVERY_PROJECT)
        memory_windows.fill(_bounded(db))
    else:
        with _other(db) as a:
            assert _held_batch(a)
            t, out = _in_a_thread(db, lambda s: facts.forget_memory(
                s, tenant.id, m.id, project_id=EVERY_PROJECT))
            time.sleep(1.0)
            alive = t.is_alive()
            a.commit()
        t.join(30)
        assert alive, "the forget did not wait for the memory the backfill holds"
        assert "e" not in out and out.get("r"), out
    assert rows(db, m.id) == []
    assert memory_windows.backlog(db)["pending"] == 0
    monkeypatch.setattr(manage, "_session", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    assert manage.memory_windows(apply=True) == 0


def test_an_edit_while_a_window_vector_is_computed(db, tenant, monkeypatch, on):
    """The worker computes a window's vector of the old text while an edit
    lands: the new windows get the new text's vectors, never the old one."""
    e = Table()
    pid = seeded_project_id(db, tenant.id)
    edited = LONG.replace("zebra", "horse")
    fired = {}
    m = None

    def hook(text, task=None, **kw):
        if m is not None and not fired and "zebra" in text and text not in (LONG, QUESTION):
            fired["at"] = text
            with _other(db) as s:
                _bounded(s)
                AgentMemoryEngine(s, tenant.id, pid).update(str(m.id), edited)
        return e(text, task, **kw)

    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
    for p in ("korely_agent.core.llm.embed", "korely_agent.services.memory_engine.embed",
              "korely_agent.services.context.embed"):
        monkeypatch.setattr(p, hook)
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    drained(db)
    assert fired, "the edit never landed during a window's vector"
    db.expire_all()
    rows = windows(db, m.id)
    assert [(w.start_char, w.end_char) for w in rows] == memory_windows.windows_of(edited)
    assert all(w.embedding is not None and abs(float(w.embedding[0]) - unit(0.9)[0]) > 1e-3
               for w in rows), "a window kept the old text's vector"


# ── the setting off, with windows stored ────────────────────────────────────

def test_off_no_search_reads_and_no_worker_computes_the_stored_windows(db, tenant, embedder,
                                                                        monkeypatch):
    setting(monkeypatch, True)
    engine(db, tenant).add(LONG, end_user_id="maria")
    engine(db, tenant).add(SHORT, end_user_id="maria")
    drained(db)
    hits, found = search(db, tenant)
    assert [h["content"] for h in hits] == [LONG, SHORT] and found     # read when on
    setting(monkeypatch, False)
    hits, found = search(db, tenant)
    assert [h["content"] for h in hits] == [SHORT, LONG] and found == {}
    assert hits[1]["score"] == pytest.approx(0.4, abs=1e-4)
    db.query(AgentMemoryWindow).update({"embedding_status": "pending", "embedding": None},
                                       synchronize_session=False)
    db.commit()
    before = len(embedder.calls)
    assert vectors.process_pending_windows(db) == {}
    assert len(embedder.calls) == before


# ── the pace of the worker ──────────────────────────────────────────────────

def test_a_backlog_never_runs_back_to_back(db, tenant, embedder, on):
    """A full pass answers no `more`: the worker waits VECTORS_EVERY before
    the next one (worker._loop), whatever is left."""
    for i in range(3):
        engine(db, tenant).add(LONG + f" Note {i}.", end_user_id="maria")
    out = vectors.process_pending_windows(db, limit=2)
    assert out["processed"] == 2 and "more" not in out, out
    assert memory_windows.backlog(db)["pending"] > 0


def test_the_worker_job_takes_its_own_limit(db, tenant, embedder, monkeypatch, on):
    from korely_agent import worker

    monkeypatch.setattr(memory_windows, "PER_PASS", 1)
    engine(db, tenant).add(LONG, end_user_id="maria")
    assert worker._windows(db)["processed"] == 1


# ── the backfill and the forgotten ──────────────────────────────────────────

def test_the_backfill_skips_a_forgotten_memory(db, tenant, embedder, monkeypatch):
    from korely_agent.services.facts import service as facts

    setting(monkeypatch, False)
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    facts.forget_memory(db, tenant.id, m.id, project_id=EVERY_PROJECT)
    setting(monkeypatch, True)
    memory_windows.fill(db)
    assert windows(db, m.id) == []


def test_recompute_counts_no_window_of_a_forgotten_memory(db, tenant, embedder, on):
    """`recompute-vectors` reads the windows of live memories only
    (vectors._live): a window left on a memory forgotten without the forget
    (a row restored by hand) is neither counted nor recomputed."""
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    drained(db)
    db.query(AgentMemory).filter(AgentMemory.id == m.id).update(
        {"deleted_at": datetime.now(timezone.utc)}, synchronize_session=False)
    db.commit()
    assert windows(db, m.id)
    assert vectors.to_recompute(db)["windows"] == 0
    assert vectors.mark_stale(db)["windows"] == 0


# ── the score ───────────────────────────────────────────────────────────────

def test_the_memorys_own_vector_when_it_is_nearer(db, tenant, embedder, on):
    """MaxP takes the better of the two: a memory whose own vector is nearer
    than its windows keeps its own score."""
    embedder.vectors[LONG] = unit(0.95)
    engine(db, tenant).add(LONG, end_user_id="maria")
    drained(db)
    hits, _found = search(db, tenant)
    assert hits[0]["content"] == LONG and hits[0]["score"] == pytest.approx(0.95, abs=1e-4)


# ── the walls ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("wall", ["agent", "project", "run", "metadata", "sessions",
                                  "received_by", "control"])
def test_every_wall_of_the_search_holds_on_the_windows(db, tenant, embedder, on, wall):
    from korely_agent.services import projects

    pid = seeded_project_id(db, tenant.id)
    kw, short_kw = {}, {}
    if wall == "project":
        beta = projects.create_project(db, tenant_id=tenant.id, name="Beta")
        db.commit()
        AgentMemoryEngine(db, tenant.id, beta.id).add(LONG, end_user_id="maria")
    elif wall == "agent":
        engine(db, tenant).add(LONG, end_user_id="maria", agent_id="sales")
        kw = short_kw = {"agent_id": "support"}
    elif wall == "run":
        engine(db, tenant).add(LONG, end_user_id="maria", run_id="r2")
        kw = short_kw = {"run_id": "r1"}
    elif wall == "metadata":
        engine(db, tenant).add(LONG, end_user_id="maria", metadata={"k": "a"})
        kw = short_kw = {"metadata": {"k": "b"}}
    elif wall == "sessions":
        engine(db, tenant).add(LONG, end_user_id="maria", kind="session")
        kw = {"exclude_sessions": True}
    elif wall == "received_by":
        kw = {"received_by": datetime.now(timezone.utc) - timedelta(days=1)}
        engine(db, tenant).add(LONG, end_user_id="maria")
    else:
        engine(db, tenant).add(LONG, end_user_id="maria")
    engine(db, tenant).add(SHORT, end_user_id="maria", **short_kw)
    drained(db)
    if wall == "received_by":
        db.query(AgentMemory).filter(AgentMemory.content == SHORT).update(
            {"created_at": datetime.now(timezone.utc) - timedelta(days=2)},
            synchronize_session=False)
        db.commit()
    found = {}
    hits = AgentMemoryEngine(db, tenant.id, pid).search(
        QUESTION, query_vector=Q, limit=10, windows=found, end_user_id="maria", **kw)
    got = [h["content"] for h in hits]
    if wall == "control":
        assert got[0] == LONG and found
    else:
        assert got == [SHORT] and found == {}, (wall, got, found)


def test_other_accounts_nearer_windows_do_not_hide_an_accounts_own(db, tenant, embedder, on):
    """The scan of the windows is behind the account's walls, not only the
    memories it brings: an account's long memory that only its window
    brings into the pool (more of its own memories are nearer by their own
    vector than the pool holds) is found, though another account has more
    windows nearer to the question than the scan reads (WINDOWS_PER_MEMORY
    for each place of the pool)."""
    other = Tenant(id=uuid.uuid4(), name="Other")
    db.add(other)
    db.commit()
    near = " ".join(f"other{i}" for i in range(memory_windows.WINDOW_WORDS * 3))
    for s, e in memory_windows.windows_of(near):
        embedder.vectors[near[s:e]] = unit(0.97)
    pool = 60        # memory_engine.search_pool(10)
    needed = pool * memory_windows.WINDOWS_PER_MEMORY // len(memory_windows.windows_of(near)) + 2
    for i in range(needed):
        engine(db, other).add(near, end_user_id=f"u{i}")
    for i in range(pool + 10):
        note = f"A short note number {i}."
        embedder.vectors[note] = unit(0.5)
        engine(db, tenant).add(note, end_user_id="maria")
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    drained(db)
    hits, found = search(db, tenant)
    assert hits[0]["content"] == LONG and str(m.id) in found, [h["content"] for h in hits[:3]]


# ── the offsets ─────────────────────────────────────────────────────────────

def test_the_offsets_hold_in_mixed_scripts(db, tenant, embedder, on):
    w100 = " ".join(f"w{i}" for i in range(memory_windows.WINDOW_WORDS))
    assert memory_windows.windows_of(w100) == []
    got = memory_windows.windows_of(w100 + " wlast")
    assert len(got) == 2 and (w100 + " wlast")[got[-1][0]:got[-1][1]].split()[-1] == "wlast"
    mixed = " ".join(["café́", "👩‍💻", "東京", "שלום", "x"] * 60)
    m = engine(db, tenant).add(mixed, end_user_id="maria")
    db.expire_all()
    stored = db.get(AgentMemory, m.id).content
    rows = windows(db, m.id)
    assert rows and [(w.start_char, w.end_char) for w in rows] == memory_windows.windows_of(stored)
    for w in rows:
        piece = stored[w.start_char:w.end_char]
        assert piece and not piece[0].isspace() and not piece[-1].isspace()
    engine(db, tenant).update(str(m.id), EDITED_SHORT)
    assert windows(db, m.id) == []
