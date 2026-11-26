"""The windows of a long memory in the store (1.0.33, MEMORY_WINDOWS, off by
default; services/memory_windows.py): written with the memory, given their
vectors by the worker, read by the search, and gone with the words.

A window's vector is derived from the person's words, so it goes with every
change of them: the edit (the setting on or off), the forget, the erasure of
the memory, of its end user, of an agent namespace, of a project, the end of
an account, and the replay of an erasure after a restore. The search ranks
a memory by the better of its own vector and its best window's (Callan,
SIGIR 1994, section 2.3; Dai and Callan, SIGIR 2019, section 3, MaxP), only
behind the walls of every read. The windows of the memories stored before
are made by `manage memory-windows`, in batches, resumable.
"""
import math
import uuid

import pytest
from sqlalchemy import delete

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory, AgentMemoryWindow, Tenant
from korely_agent.services import memory_windows, vector_state, vectors
from korely_agent.services.memory_engine import AgentMemoryEngine
from korely_agent.services.sentinels import EVERY_PROJECT
from tests.conftest import seeded_project_id


def unit(cos: float) -> list:
    """A vector whose cosine to Q is `cos`."""
    v = [0.0] * EMBEDDING_DIM
    v[0] = cos
    v[1] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return v


Q = unit(1.0)
QUESTION = "Where did the zebra go?"
WORDS = " ".join(f"word{i}" for i in range(memory_windows.WINDOW_WORDS + 40))
LONG = f"{WORDS}. The zebra went to the river at noon on Tuesday."
SHORT = "A short note about the weather."


class Table:
    """The embedder of these tests: a vector per text, the others far."""

    def __init__(self):
        self.vectors = {QUESTION: Q, SHORT: unit(0.6), LONG: unit(0.4)}
        for s, e in memory_windows.windows_of(LONG):
            self.vectors[LONG[s:e]] = unit(0.9 if "zebra" in LONG[s:e] else 0.1)
        self.calls = []

    def __call__(self, text, task=None, **kw):
        self.calls.append(text)
        return list(self.vectors.get(text, unit(0.0)))


@pytest.fixture
def embedder(monkeypatch):
    e = Table()
    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
    monkeypatch.setattr("korely_agent.core.llm.embed", e)
    monkeypatch.setattr("korely_agent.services.memory_engine.embed", e)
    monkeypatch.setattr("korely_agent.services.context.embed", e)
    return e


def setting(monkeypatch, on: bool) -> None:
    from korely_agent.config import get_settings

    monkeypatch.setenv("MEMORY_WINDOWS", "true" if on else "false")
    get_settings.cache_clear()


@pytest.fixture
def on(monkeypatch):
    setting(monkeypatch, True)


def engine(db, tenant):
    return AgentMemoryEngine(db, tenant.id, seeded_project_id(db, tenant.id))


def rows(db, memory_id=None):
    """Every row of the windows' table, the `NONE` row of a short memory too."""
    db.expire_all()
    q = db.query(AgentMemoryWindow)
    if memory_id is not None:
        q = q.filter(AgentMemoryWindow.memory_id == memory_id)
    return q.order_by(AgentMemoryWindow.ordinal).all()


def windows(db, memory_id=None):
    """The windows, without the `NONE` row of a short memory."""
    return [w for w in rows(db, memory_id) if w.embedding_status != memory_windows.NONE]


def drained(db):
    while vectors.process_pending_windows(db, limit=50):
        pass


def search(db, tenant, **kw):
    found = {}
    hits = engine(db, tenant).search(QUESTION, query_vector=Q, limit=10, windows=found, **kw)
    return hits, found


# ── the write and the drain ─────────────────────────────────────────────────

def test_a_long_memory_is_written_with_its_windows_waiting(db, tenant, embedder, on):
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    s = engine(db, tenant).add(SHORT, end_user_id="maria")
    rows = windows(db, m.id)
    assert [(w.start_char, w.end_char) for w in rows] == memory_windows.windows_of(LONG)
    assert all(w.embedding_status == vector_state.PENDING and w.tenant_id == tenant.id
               for w in rows)
    assert windows(db, s.id) == []
    # The write computed the memory's own vector, never a window's.
    assert not any(LONG[a:b] in embedder.calls for a, b in memory_windows.windows_of(LONG))
    drained(db)
    assert all(w.embedding_status == vector_state.DONE for w in windows(db, m.id))


def test_with_the_setting_off_nothing_is_written_computed_or_read(db, tenant, embedder,
                                                                  monkeypatch):
    setting(monkeypatch, False)
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    engine(db, tenant).add(SHORT, end_user_id="maria")
    assert rows(db) == []           # no window, no NONE row either
    assert vectors.process_pending_windows(db) == {}
    hits, found = search(db, tenant)
    assert [h["content"] for h in hits] == [SHORT, LONG] and found == {}
    assert hits[1]["score"] == pytest.approx(0.4, abs=1e-4)
    engine(db, tenant).update(str(m.id), SHORT + " Edited.")
    assert rows(db) == []


# ── the search ──────────────────────────────────────────────────────────────

def test_a_memory_is_ranked_by_its_best_window(db, tenant, embedder, on):
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    engine(db, tenant).add(SHORT, end_user_id="maria")
    drained(db)
    hits, found = search(db, tenant)
    assert [h["content"] for h in hits] == [LONG, SHORT]
    assert hits[0]["score"] == pytest.approx(0.9, abs=1e-4)
    s, e = found[str(m.id)]
    assert "zebra" in LONG[s:e] and str(hits[1]["id"]) not in found
    # The floor reads the score the search answers, the window's.
    hits, _ = search(db, tenant, min_score=0.85)
    assert [h["content"] for h in hits] == [LONG]


def test_a_window_without_its_vector_ranks_nothing(db, tenant, embedder, on):
    engine(db, tenant).add(LONG, end_user_id="maria")
    engine(db, tenant).add(SHORT, end_user_id="maria")
    hits, found = search(db, tenant)
    assert [h["content"] for h in hits] == [SHORT, LONG] and found == {}


def test_the_windows_are_behind_every_wall_of_the_search(db, tenant, embedder, on):
    """Another account's long memory, another end user's, a forgotten one:
    none is brought by its windows."""
    other = Tenant(id=uuid.uuid4(), name="Other")
    db.add(other)
    db.commit()
    engine(db, other).add(LONG, end_user_id="maria")
    engine(db, tenant).add(LONG, end_user_id="luca")
    gone = engine(db, tenant).add(LONG, end_user_id="maria")
    engine(db, tenant).add(SHORT, end_user_id="maria")
    drained(db)
    from korely_agent.services.facts import service as facts

    facts.forget_memory(db, tenant.id, gone.id, project_id=EVERY_PROJECT)
    hits, found = search(db, tenant, end_user_id="maria")
    assert [h["content"] for h in hits] == [SHORT] and found == {}


# ── the windows go with the words ───────────────────────────────────────────

@pytest.mark.parametrize("on_at_the_edit", [True, False], ids=["on", "off"])
def test_an_edit_replaces_the_windows(db, tenant, embedder, monkeypatch, on_at_the_edit):
    setting(monkeypatch, True)
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    drained(db)
    before = {w.id for w in windows(db, m.id)}
    setting(monkeypatch, on_at_the_edit)
    edited = LONG.replace("zebra", "horse")
    engine(db, tenant).update(str(m.id), edited)
    rows = windows(db, m.id)
    assert not before & {w.id for w in rows}
    if on_at_the_edit:
        assert [(w.start_char, w.end_char) for w in rows] == memory_windows.windows_of(edited)
        assert all(w.embedding is None and w.embedding_status == vector_state.PENDING
                   for w in rows)
    else:
        assert rows == []


def test_the_same_text_keeps_its_windows(db, tenant, embedder, on):
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    drained(db)
    before = [(w.id, w.embedding_status) for w in windows(db, m.id)]
    db.query(AgentMemory).filter(AgentMemory.id == m.id).update(
        {"graph_sync_status": "pending"}, synchronize_session=False)
    db.commit()
    engine(db, tenant).update(str(m.id), LONG)
    assert [(w.id, w.embedding_status) for w in windows(db, m.id)] == before


@pytest.fixture
def written(db, tenant, embedder, on):
    m = engine(db, tenant).add(LONG, end_user_id="maria", agent_id="support")
    drained(db)
    assert windows(db, m.id)
    return m


def test_a_forget_takes_the_windows(db, tenant, written):
    from korely_agent.services.facts import service as facts

    short = engine(db, tenant).add(SHORT, end_user_id="maria")
    assert [w.embedding_status for w in rows(db, short.id)] == [memory_windows.NONE]
    facts.forget_memory(db, tenant.id, written.id, project_id=EVERY_PROJECT)
    facts.forget_memory(db, tenant.id, short.id, project_id=EVERY_PROJECT)
    assert rows(db) == []
    db.expire_all()
    assert db.get(AgentMemory, written.id).deleted_at is not None


def test_the_engines_delete_takes_the_windows(db, tenant, written):
    assert engine(db, tenant).delete(str(written.id))
    assert windows(db) == []


def test_an_erasure_of_the_memory_takes_the_windows(db, tenant, written):
    from korely_agent.services.facts import service as facts

    assert facts.erase_memory(db, tenant.id, written.id, project_id=EVERY_PROJECT)
    assert windows(db) == []


def test_an_erasure_of_its_end_user_takes_the_windows(db, tenant, written):
    from korely_agent.services.facts import service as facts

    engine(db, tenant).add(SHORT, end_user_id="maria")
    facts.forget_end_user(db, tenant.id, "maria", project_id=EVERY_PROJECT)
    assert rows(db) == []


def test_an_erasure_of_its_agent_namespace_takes_the_windows(db, tenant, written):
    from korely_agent.services.facts import service as facts

    facts.delete_agent(db, tenant.id, "support", project_id=EVERY_PROJECT)
    assert windows(db) == []


def test_an_erasure_of_its_project_takes_the_windows(db, tenant, embedder, on):
    from korely_agent.services import projects

    beta = projects.create_project(db, tenant_id=tenant.id, name="Beta")
    db.commit()
    m = AgentMemoryEngine(db, tenant.id, beta.id).add(LONG, end_user_id="maria")
    assert windows(db, m.id)
    projects.delete_project(db, tenant.id, beta, erase=True)
    assert windows(db) == []


def test_the_end_of_an_account_takes_the_windows(db, tenant, written):
    """The account's row deleted, as the Cloud's close and its retention
    round delete it (hosted/closing.py, `delete(Tenant)`): every row of it
    goes by the foreign keys, the windows too."""
    db.execute(delete(Tenant).where(Tenant.id == tenant.id))
    db.commit()
    assert windows(db) == []


def test_a_forget_made_again_after_a_restore_takes_the_windows(db, tenant, written):
    """A dump taken before a forget, restored: the replay of the forget's
    record (services/erasure_replay.py) forgets the memory again, and its
    windows, which the dump held, go again."""
    from korely_agent.db.models import AgentForgetAudit
    from korely_agent.services import erasure_replay
    from korely_agent.services.facts import service as facts

    facts.forget_memory(db, tenant.id, written.id, project_id=EVERY_PROJECT)
    records = erasure_replay.export(db)
    assert [r["scope"] for r in records] == ["memory"]
    # The restore: the database as the dump held it, before the forget.
    db.query(AgentForgetAudit).delete(synchronize_session=False)
    db.query(AgentMemory).filter(AgentMemory.id == written.id).update(
        {"deleted_at": None, "graph_sync_status": "done"}, synchronize_session=False)
    db.commit()
    memory_windows.make(db, db.get(AgentMemory, written.id))
    db.commit()
    assert windows(db, written.id)
    out = erasure_replay.replay(db, records)
    assert out["replayed"] == {"memory": 1}, out
    assert windows(db) == []


def test_a_short_memory_is_read_once_by_the_backfill(db, tenant, embedder, monkeypatch):
    """A memory too short for a window gets one `NONE` row: the next run
    neither reads nor holds it again; an edit of its text takes the row, and
    the next run reads it again."""
    setting(monkeypatch, False)
    short = [engine(db, tenant).add(f"{SHORT} Number {i}.", end_user_id="maria")
             for i in range(5)]
    long_ = engine(db, tenant).add(LONG, end_user_id="maria")
    setting(monkeypatch, True)
    first = memory_windows.fill(db, batch=2)
    assert first["read"] == 6 and first["memories"] == 1
    assert all([(w.embedding_status, w.start_char, w.end_char) for w in rows(db, m.id)]
               == [(memory_windows.NONE, 0, 0)] for m in short)
    assert memory_windows.fill(db)["read"] == 0
    assert memory_windows.backlog(db)["short"] == 5
    setting(monkeypatch, False)
    engine(db, tenant).update(str(short[0].id), LONG + " Now long.")
    setting(monkeypatch, True)
    again = memory_windows.fill(db)
    assert again["read"] == 1 and again["memories"] == 1
    assert windows(db, short[0].id) and windows(db, long_.id)
    # No drain, search or recompute reads a NONE row.
    drained(db)
    assert all(w.embedding is None for m in short[1:] for w in rows(db, m.id))
    assert vectors.to_recompute(db)["windows"] == len(windows(db))


@pytest.mark.parametrize("on_at_the_edits", [True, False], ids=["on", "off"])
def test_a_short_memory_made_long_and_short_again(db, tenant, embedder, monkeypatch,
                                                  on_at_the_edits):
    """Its NONE row gives way to windows and back with the setting on, and
    goes with the first edit with the setting off."""
    setting(monkeypatch, True)
    m = engine(db, tenant).add(SHORT, end_user_id="maria")
    setting(monkeypatch, on_at_the_edits)
    engine(db, tenant).update(str(m.id), LONG)
    states = [w.embedding_status for w in rows(db, m.id)]
    assert states == (["pending"] * len(memory_windows.windows_of(LONG)) if on_at_the_edits
                      else [])
    engine(db, tenant).update(str(m.id), SHORT + " Edited.")
    assert [(w.embedding_status, w.start_char, w.end_char) for w in rows(db, m.id)] == \
        ([(memory_windows.NONE, 0, 0)] if on_at_the_edits else [])


def test_the_backlog_counts_the_none_rows_apart(db, tenant, embedder, on):
    engine(db, tenant).add(SHORT, end_user_id="maria")
    engine(db, tenant).add(LONG, end_user_id="maria")
    out = memory_windows.backlog(db)
    n = len(memory_windows.windows_of(LONG))
    assert out == {"windows": n, "pending": n, "done": 0, "error": 0, "short": 1}


def test_a_none_row_is_never_a_window_of_the_search(db, tenant, embedder, on):
    """A NONE row given a vector by hand (the product never writes one) is
    still no window: the search reads the windows by their state too."""
    s = engine(db, tenant).add(SHORT, end_user_id="maria")
    engine(db, tenant).add(LONG, end_user_id="maria")
    drained(db)
    db.query(AgentMemoryWindow).filter(AgentMemoryWindow.memory_id == s.id).update(
        {"embedding": Q}, synchronize_session=False)
    db.commit()
    _hits, found = search(db, tenant)
    assert str(s.id) not in found


# ── another embedding model, and the memories stored before ─────────────────

def test_recompute_vectors_takes_the_windows_too(db, tenant, written):
    n = len(windows(db, written.id))
    assert vectors.to_recompute(db)["windows"] == n
    assert vectors.mark_stale(db)["windows"] == n
    out = vectors.recompute(db)
    assert out["windows"]["recomputed"] == n
    assert all(w.embedding_status == vector_state.DONE for w in windows(db))


def test_the_memories_stored_before_get_their_windows_in_batches(db, tenant, embedder,
                                                                 monkeypatch):
    setting(monkeypatch, False)
    ids = [engine(db, tenant).add(LONG + f" Note {i}.", end_user_id="maria").id
           for i in range(3)]
    engine(db, tenant).add(SHORT, end_user_id="maria")
    assert windows(db) == []
    setting(monkeypatch, True)
    first = memory_windows.fill(db, batch=1, limit=2)
    assert first["read"] == 2
    made = {w.memory_id for w in windows(db)}
    again = memory_windows.fill(db, batch=1)
    assert again["memories"] == 3 - len(made & set(ids)), (first, again)
    assert {w.memory_id for w in windows(db)} == set(ids)
    assert memory_windows.fill(db, batch=10)["windows"] == 0


def test_the_command_makes_rows_only_and_refuses_with_the_setting_off(db, tenant, embedder,
                                                                     monkeypatch, capsys):
    """`manage memory-windows --apply` makes the windows and computes no
    vector (the worker's job does, at its pace): no model in the command's
    process, no race with the worker for the same rows."""
    from korely_agent import manage

    monkeypatch.setattr(manage, "_session", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    setting(monkeypatch, False)
    m = engine(db, tenant).add(LONG, end_user_id="maria")
    assert manage.memory_windows(apply=True) == 1
    assert windows(db) == []
    assert manage.memory_windows(apply=False) == 0
    setting(monkeypatch, True)
    before = len(embedder.calls)
    assert manage.memory_windows(apply=True, batch=2) == 0
    assert len(embedder.calls) == before
    rows = windows(db, m.id)
    assert rows and all(w.embedding_status == vector_state.PENDING for w in rows)
    drained(db)
    assert all(w.embedding_status == vector_state.DONE for w in windows(db, m.id))
    # A window that gave up its vector is queued again by the next run.
    db.query(AgentMemoryWindow).update({"embedding_status": vector_state.ERROR,
                                        "embedding": None}, synchronize_session=False)
    db.commit()
    assert manage.memory_windows(apply=True, batch=2) == 0
    assert all(w.embedding_status == vector_state.PENDING for w in windows(db, m.id))
    out = capsys.readouterr().out
    assert "MEMORY_WINDOWS=true" in out and "windows made" in out and "waiting for" in out


# ── the block ───────────────────────────────────────────────────────────────

def test_the_block_shows_a_long_note_by_its_best_window(db, tenant, embedder, on):
    """An agent's long note, in a block too small for it: the sentence of
    its best window, never its opening."""
    from korely_agent.services.context import assemble_context

    engine(db, tenant).add(LONG, agent_id="support")
    drained(db)
    out = assemble_context(db, tenant.id, query=QUESTION, agent_id="support",
                           token_budget=200, include_facts=False,
                           project_id=seeded_project_id(db, tenant.id))
    assert "The zebra went to the river" in out["context"], out["context"]
    assert "word0 " not in out["context"] and out["memories_left_out"] == []
