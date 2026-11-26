"""A vector for each window of a long memory (1.0.33, off by default:
MEMORY_WINDOWS).

Why. A memory has one vector (memory_engine.memory_vector), computed from its
first 8,000 characters, so a long memory is found by what most of it is
about: a question about one sentence in its middle or at its end can find
other memories first, and the context block, which cuts a memory too long
for its room, shows its opening.

The rule is passage-level evidence: a long text is also cut in overlapping
windows of a fixed number of words, each with its own score, and the text
is ranked by its best window.
  * Callan, "Passage-Level Evidence in Document Retrieval" (SIGIR 1994),
    section 2.3: windows of n words, "creating new passages of length n
    every n/2 words", so "a small block of relevant text" is not split
    between two; ranking documents by their best passage was 20.7% better
    than by the whole document on the long documents of the Federal
    Register (Table 3). Section 2.3.1: the best size depends on the
    collection, 150 to 300 words on long documents.
  * Dai and Callan, "Deeper Text Understanding for IR with Contextual Neural
    Language Modeling" (SIGIR 2019), section 3: a document cut in
    overlapping passages and scored by its best passage (MaxP); section 4:
    windows of 150 words with a stride of 75.
  * Wu et al., "LongMemEval" (ICLR 2025), section 5.3: a memory found by
    several keys and given back once ("indexing-stage merging") beat
    merging the rankings of separate searches.
A memory is ranked by the better of its own vector and its best window
(the max of the two: a short memory, or one about one thing, keeps its own
score), and given back once, with its whole text: the window chooses where
the context block looks when the memory does not fit (services/context.py),
never what is stored.

The window. WINDOW_WORDS words (100), the next one beginning STRIDE_WORDS
(half a window, 50) after the one before: Callan's overlap and Dai and
Callan's stride. Smaller windows were measured too (40, 60 and 80 words, on
one store of agent notes, with no reader: docs/guide/limits.md): a window is
shorter and more focused than its memory, its cosine to a short question is
higher, and the best of many windows comes closer than the best of few, so
small windows ranked long memories high for their length. A word is a run
of letters and digits in any script; in a script written without spaces it
is a pair of letters next to each other, counted without overlap: Kwok's
bigram ("Lexicon Effects on Chinese Information Retrieval", 1997), which
services/excerpt.py uses as a term for those scripts, here as a count and
not a term (`excerpt.units`): no dictionary, no list of languages. A memory of WINDOW_WORDS words or fewer has no window: its own
vector is its one window, and it gets one row that says so (`NONE`: no
offsets, no vector), so the backfill reads it once and never again until
its text changes. The windows cover the whole text, up to
limits.MAX_CONTENT_CHARS: a long memory is read in windows, never cut.

What is stored. Two offsets per window into the memory's current text and
the window's vector (db/models.AgentMemoryWindow), never a copy of the
words. The vector is derived from the person's words: it goes with every
change of them, the setting on or off. An edit replaces the windows
(`after_the_text`), a forget deletes them (`drop`); an erasure of the
memory, of its end user, of an agent namespace, of a project, and the end
of an account delete the memory's row, and its windows with it (ON DELETE
CASCADE); a replay of erasures after a restore repeats those same
functions.

Who writes them, and the lock. A memory's windows are written only by a
transaction that holds the memory's row: the write that makes the row, the
edit (its compare-and-set on the row), and `fill`, which takes each row FOR
NO KEY UPDATE SKIP LOCKED and reads the text and the forget under that lock.
So an edit or a forget of a memory the backfill holds waits for its commit
and then replaces or deletes what it made, a memory an edit holds is left to
the edit, and two backfills never make one memory's windows twice.

The cost. One vector per window, computed by the worker's job `windows`
(services/vectors.process_pending_windows), never on a request: at most
PER_PASS windows a pass, and the next pass VECTORS_EVERY seconds after the
end of the one before, whatever is left. With the local embedder
that is this machine's CPU and no model call; with EMBEDDING_PROVIDER=gemini
each window is one call to Google, through the same gate and the same bill
as every vector (core/llm.embed). `manage memory-windows --apply` makes the
windows of the memories stored before and leaves their vectors to that job.
"""
from __future__ import annotations

import uuid
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import exists
from sqlalchemy.orm import Session

from ..db.models import AgentMemory, AgentMemoryWindow
from . import excerpt

#: The words of a window (the module's docstring), and how many words one
#: window begins after the one before it: half a window, so a stretch of
#: text cut by one window is whole in the next (Callan 1994, section 2.3).
WINDOW_WORDS = 100
STRIDE_WORDS = WINDOW_WORDS // 2

#: The most windows' vectors one pass of the worker's job computes
#: (vectors.process_pending_windows), a pass every worker.VECTORS_EVERY
#: seconds after the end of the one before: 2,400 an hour, the bound on the
#: CPU (or, with EMBEDDING_PROVIDER=gemini, the calls to Google) the windows
#: take on an install whose CPU serves every request too, whatever the
#: backlog. A constant, not a setting: the pace is what the guides promise.
PER_PASS = 20

#: The state of the one row of a memory too short for a window: it was
#: read and needs none. No offsets, no vector; no drain, search or
#: recompute reads it (they read `pending`, `done`, `stale` and vectors);
#: it goes with the memory's text as the windows do (`drop`).
NONE = "none"

#: How many windows a search brings for each memory it asks for: a memory
#: has several, and the windows of the first memories must not take the
#: places of the others' best ones.
WINDOWS_PER_MEMORY = 4


def enabled() -> bool:
    """MEMORY_WINDOWS (config.AgentSettings.memory_windows)."""
    from ..config import get_settings

    return bool(get_settings().memory_windows)


def windows_of(text: str, size: int = WINDOW_WORDS) -> List[Tuple[int, int]]:
    """(start, end) of each window of `text`, in characters of `text`, in
    order: `size` words (`excerpt.units`), each beginning `size // 2` words
    after the one before, the last one ending at the text's last word.
    Empty for a text of `size` words or fewer: its own vector is its one
    window."""
    words = excerpt.units(text or "")
    n = len(words)
    if size < 1 or n <= size:
        return []
    stride = max(1, size // 2)
    out: List[Tuple[int, int]] = []
    i = 0
    while True:
        last = min(i + size, n) - 1
        out.append((words[i][0], words[last][1]))
        if i + size >= n:
            return out
        i += stride


# ── the write ───────────────────────────────────────────────────────────────

def drop(db: Session, memory_ids: Iterable) -> int:
    """The windows of these memories deleted, vectors and all; not
    committed. Every path that changes a memory's text, or forgets it,
    calls it whatever the setting says: windows made while it was on are
    the person's words too."""
    ids = [m if isinstance(m, uuid.UUID) else uuid.UUID(str(m)) for m in memory_ids]
    if not ids:
        return 0
    return int(db.query(AgentMemoryWindow).filter(AgentMemoryWindow.memory_id.in_(ids))
               .delete(synchronize_session=False) or 0)


def make(db: Session, m: AgentMemory) -> int:
    """The windows of `m`'s current text, each waiting for its vector, or,
    for a text too short for one, its one `NONE` row; not committed. `m` has
    its id (flushed, or given). How many windows (0 for a `NONE` row)."""
    spans = windows_of(m.content or "")
    rows = [dict(id=uuid.uuid4(), tenant_id=m.tenant_id, memory_id=m.id, ordinal=i,
                 start_char=a, end_char=b, embedding_attempts=0)
            for i, (a, b) in enumerate(spans)]
    if not rows:
        rows = [dict(id=uuid.uuid4(), tenant_id=m.tenant_id, memory_id=m.id, ordinal=0,
                     start_char=0, end_char=0, embedding_attempts=0, embedding_status=NONE)]
    db.execute(AgentMemoryWindow.__table__.insert(), rows)
    return len(spans)


def after_the_text(db: Session, m: AgentMemory, *, changed: bool) -> int:
    """The write's step (memory_engine.add and update), in its transaction:
    when the text changed, the old windows go, setting on or off; with the
    setting on, the windows of the text as it is now are made when it has
    none. How many were made."""
    if changed:
        drop(db, [m.id])
    if not enabled():
        return 0
    if not changed and db.query(exists().where(AgentMemoryWindow.memory_id == m.id)).scalar():
        return 0
    return make(db, m)


# ── the search ──────────────────────────────────────────────────────────────

def ranked(db: Session, base, query_vector, by_meaning: list, pool: int,
           best: Optional[Dict[str, Tuple[int, int]]] = None) -> list:
    """`by_meaning` (the memories of `base` nearest to the question by their
    own vector, (memory, distance) by distance) ranked again by the better
    of their own distance and their best window's, with the memories whose
    windows are nearer than that: (memory, distance), the nearer first, the
    id after, at most `pool`. `best`, when given, gets each memory's best
    window, (start, end) in its text, by its id.

    Exact for the windows it reads: the `pool` * WINDOWS_PER_MEMORY windows
    nearest to the question behind `base`'s walls (the memory live and
    readable by the caller, its own vector computed), then every window of
    each memory found either way, so the best window of a memory is its best
    one, not the best one the first scan met."""
    from .memory_core.vector_search import hnsw_depth

    W = AgentMemoryWindow
    wdist = W.embedding.cosine_distance(query_vector).label("wdist")
    scan = min(1000, max(1, int(pool)) * WINDOWS_PER_MEMORY)
    hnsw_depth(db, scan)
    # A window with its vector, never a NONE row, whatever a hand put on it:
    # the state is the rule, the vector's presence a second guard.
    near = (base.join(W, W.memory_id == AgentMemory.id)
            .filter(W.embedding.isnot(None), W.embedding_status != NONE)
            .with_entities(W.memory_id, wdist)
            .order_by(wdist, W.id).limit(scan).all())
    own = {m.id: (m, float(d)) for m, d in by_meaning}
    missing = list({mid for mid, _d in near} - set(own))
    if missing:
        dist = AgentMemory.embedding.cosine_distance(query_vector).label("dist")
        for m, d in base.filter(AgentMemory.id.in_(missing)).add_columns(dist).all():
            own[m.id] = (m, float(d))
    if not own:
        return []
    windows = (db.query(W.memory_id, W.start_char, W.end_char, wdist)
               .filter(W.memory_id.in_(list(own)), W.embedding.isnot(None),
                       W.embedding_status != NONE)
               .order_by(W.memory_id, wdist, W.id).distinct(W.memory_id).all())
    nearest = {r.memory_id: r for r in windows}
    out = []
    for mid, (m, d) in own.items():
        w = nearest.get(mid)
        if w is not None:
            d = min(d, float(w.wdist))
            if best is not None:
                best[str(mid)] = (int(w.start_char), int(w.end_char))
        out.append((m, d))
    out.sort(key=lambda pair: (pair[1], str(pair[0].id)))
    return out[:max(1, int(pool))]


# ── the windows of the memories stored before ───────────────────────────────

def _locked_batch(db: Session, *, after: Optional[uuid.UUID], batch: int):
    """The next `batch` live memories by id after `after` that have no row
    (no window, no `NONE`), each held FOR NO KEY UPDATE SKIP LOCKED in this
    transaction:
    (rows, the last id read). The row is read under the lock: its text and
    its forget are the ones committed when the lock was taken. A memory
    another transaction holds (an edit, a forget, another backfill) is
    skipped, and the next run takes it if it still needs windows."""
    q = (db.query(AgentMemory.id, AgentMemory.tenant_id, AgentMemory.content)
         .filter(AgentMemory.deleted_at.is_(None),
                 ~exists().where(AgentMemoryWindow.memory_id == AgentMemory.id)))
    if after is not None:
        q = q.filter(AgentMemory.id > after)
    rows = (q.order_by(AgentMemory.id).limit(max(1, int(batch)))
            .with_for_update(skip_locked=True, key_share=True, of=AgentMemory).all())
    return rows, (rows[-1].id if rows else None)


def fill_batch(db: Session, *, after: Optional[uuid.UUID] = None, batch: int = 200) -> dict:
    """One batch of `fill`, in one transaction, committed: the memories of
    `_locked_batch`, each given the windows of its text (or its `NONE` row)
    unless a row of it was committed after the batch's snapshot (read again
    under the lock, a statement of its own)."""
    rows, last = _locked_batch(db, after=after, batch=batch)
    ids = [r.id for r in rows]
    have = {mid for (mid,) in db.query(AgentMemoryWindow.memory_id)
            .filter(AgentMemoryWindow.memory_id.in_(ids)).distinct()} if ids else set()
    made = memories = 0
    for r in rows:
        if r.id in have:
            continue
        n = make(db, r)
        made += n
        memories += bool(n)
    db.commit()
    return {"read": len(rows), "memories": memories, "windows": made, "last": last}


def fill(db: Session, *, batch: int = 200, limit: int = 0, progress=None) -> dict:
    """`manage memory-windows --apply`: the windows of every live memory that
    has none, `batch` memories per transaction (`fill_batch`), so a run
    stopped anywhere goes on from where it was; and the windows that gave up
    their vector, waiting again. Computes no vector: the worker's job does,
    at its pace. `limit`: at most this many memories read (0: all)."""
    read = made = memories = 0
    after = None
    while True:
        out = fill_batch(db, after=after, batch=batch)
        if not out["read"]:
            break
        after = out["last"]
        read += out["read"]
        made += out["windows"]
        memories += out["memories"]
        if progress:
            progress(read, made)
        if limit and read >= limit:
            break
    again = int(db.query(AgentMemoryWindow).filter(AgentMemoryWindow.embedding_status == "error")
                .update({"embedding_status": "pending", "embedding_attempts": 0,
                         "embedding_next_at": None}, synchronize_session=False) or 0)
    db.commit()
    return {"read": read, "memories": memories, "windows": made, "queued_again": again}


def backlog(db: Session) -> dict:
    """What the windows' table holds: windows, those still waiting for
    their vector, those that gave up, and the memories read that need none."""
    from sqlalchemy import func

    rows = dict(db.query(AgentMemoryWindow.embedding_status, func.count())
                .group_by(AgentMemoryWindow.embedding_status).all())
    db.commit()
    short = rows.pop(NONE, 0)
    return {"windows": sum(rows.values()), "pending": rows.get("pending", 0),
            "done": rows.get("done", 0), "error": rows.get("error", 0), "short": short}
