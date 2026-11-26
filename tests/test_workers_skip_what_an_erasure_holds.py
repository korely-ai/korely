"""A worker shared by every account never waits for rows one account's
erasure holds (audit of 27 Oct 2026, two rows left as known issues until
1.0.40).

An account's project erasure locks the rows it erases and keeps them to its
one commit, minutes for a large project. The facts drain's reaper, the batch
worker's read of a job's item, the vector drain's write and the webhook
round's give-back waited for such a row with no deadline, on the one thread
or the one claim that serves every account, so every account's work waited
for one account's erasure.

Here another session holds the row, as the erasure does. The worker's own
session has a lock wait of a few seconds, so the code of before fails here
(a lock timeout raised, or a job filed as failed) instead of hanging the
run; the code of now skips the row, at once or after `LOCK_WAIT_SECONDS`, and
serves the other account.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from korely_agent.core import threads
from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory, Tenant

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def other(db):
    t = Tenant(id=uuid.uuid4(), name="Other account")
    db.add(t)
    db.commit()
    return t


@pytest.fixture
def eraser(engine):
    """A session that holds rows as an erasure does, until the test ends."""
    s = sessionmaker(bind=engine)()
    yield s
    s.rollback()
    s.close()


def _hold(eraser, table, row_id):
    eraser.execute(text(f"SELECT id FROM {table} WHERE id = :i FOR UPDATE"), {"i": row_id})


def _bounded(db):
    """The worker's session waits for a lock 3 s at most: the code of before
    raises then, rather than wait for the test's end. On every transaction
    the session begins, whatever connection of the pool it is on."""
    from sqlalchemy import event

    event.listen(db, "after_begin",
                 lambda _s, _t, conn: conn.exec_driver_sql("SET LOCAL lock_timeout = '3s'"))
    db.commit()


def _without_waiting(fn, eraser, seconds=10):
    """Run `fn` in a thread with no lock bound of the test's own: whether it
    was still waiting after `seconds`, once the eraser let go and it ended."""
    import threading

    out = {}
    t = threading.Thread(target=lambda: out.setdefault("value", fn()))
    t.start()
    t.join(seconds)
    waited = t.is_alive()
    eraser.rollback()
    t.join(30)
    return waited, out.get("value")


def test_the_reaper_skips_a_stale_row_an_erasure_holds(db, tenant, other, eraser):
    from korely_agent.services.facts import drain

    held = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="a", content="held",
                       graph_sync_status="processing", graph_sync_claimed_at=NOW)
    db.add(held)
    db.add(AgentMemory(id=uuid.uuid4(), tenant_id=other.id, end_user_id="b", content="waiting",
                       graph_sync_status="pending"))
    db.commit()
    _hold(eraser, "agent_memories", held.id)
    _bounded(db)
    got = drain._claim(db, 20)
    assert [m.content for m in got] == ["waiting"]
    eraser.rollback()
    db.expire_all()
    assert db.get(AgentMemory, held.id).graph_sync_status == "processing", (
        "the held row is reaped at a later claim, not now")


def test_the_reaper_skips_a_forgotten_row_an_erasure_holds(db, tenant, other, eraser):
    """The reaper lays to rest the forgotten rows still waiting: one the
    erasure holds is skipped as well, not waited for."""
    from datetime import datetime, timezone

    from korely_agent.services.facts import drain

    gone = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="a", content="gone",
                       graph_sync_status="pending", deleted_at=datetime.now(timezone.utc))
    db.add(gone)
    db.add(AgentMemory(id=uuid.uuid4(), tenant_id=other.id, end_user_id="b", content="waiting",
                       graph_sync_status="pending"))
    db.commit()
    _hold(eraser, "agent_memories", gone.id)
    _bounded(db)
    assert [m.content for m in drain._claim(db, 20)] == ["waiting"]


def test_the_batch_reaper_skips_a_job_an_erasure_holds(db, tenant, eraser):
    from korely_agent.db.models import AgentBatchJob
    from korely_agent.services import batch

    stale = batch.enqueue_batch(db, tenant.id, [{"content": "a", "user_id": "u"}])
    stale.status = batch.STATUS_PROCESSING
    stale.updated_at = NOW
    db.commit()
    _hold(eraser, "agent_batch_jobs", stale.id)
    _bounded(db)
    assert batch.reclaim_interrupted(db) == 0
    eraser.rollback()
    assert batch.reclaim_interrupted(db) == 1, "the job was not reaped once the erasure let go"
    db.expire_all()
    assert db.get(AgentBatchJob, stale.id).status == batch.STATUS_FAILED


def test_the_worker_lock_deadline_gives_up_a_held_row_in_its_time(db, tenant, eraser,
                                                                  monkeypatch):
    """`threads.worker_lock_deadline`: a write in its block waits for a held
    row at most LOCK_WAIT_SECONDS, then raises what `deadline_passed` knows;
    out of the block the session waits as before. No bound of the test's
    own: without the deadline the write waits for the holder, and the
    thread is still waiting when the test looks."""
    import threading

    monkeypatch.setattr(threads, "LOCK_WAIT_SECONDS", 0.3)
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="a", content="x")
    db.add(m)
    db.commit()
    _hold(eraser, "agent_memories", m.id)
    out = {}

    def write():
        try:
            with threads.worker_lock_deadline(db):
                db.execute(text("UPDATE agent_memories SET content = 'y' WHERE id = :i"),
                           {"i": m.id})
                db.commit()
            out["written"] = True
        except Exception as e:  # noqa: BLE001 - what the test looks at
            db.rollback()
            out["deadline"] = threads.deadline_passed(e)

    t = threading.Thread(target=write)
    t.start()
    t.join(10)
    waited = t.is_alive()
    eraser.rollback()          # lets a write that waits go on, so the test ends
    t.join(10)
    assert not waited, "the write waited for the held row with no deadline"
    assert out == {"deadline": True}
    with db.connection() as conn:
        assert conn.exec_driver_sql("SHOW lock_timeout").scalar() in ("0", "0ms"), (
            "the deadline outlived its block")


def test_the_batch_worker_gives_back_a_job_an_erasure_holds_and_serves_the_next(
        db, tenant, other, eraser, monkeypatch):
    from korely_agent.db.models import AgentBatchJob
    from korely_agent.services import batch

    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                        lambda *a, **k: [])
    written = []
    monkeypatch.setattr(batch, "_import_one",
                        lambda db_, tenant_id, item, **kw: (written.append(tenant_id),
                                                            (str(uuid.uuid4()), None))[1])
    mine = batch.enqueue_batch(db, tenant.id, [{"content": "a", "user_id": "u"}])
    theirs = batch.enqueue_batch(db, other.id, [{"content": "b", "user_id": "u"}])
    db.commit()
    turns = threads.Rotation()
    turns.first([tenant.id, other.id], 2)
    turns.served([other.id])                # the held account's job is worked first
    monkeypatch.setattr(batch, "_turns", turns, raising=False)
    real = batch._work_a_slice
    mine_id = mine.id

    def erasure_starts(db_, job, items_max, job_id, *claim):
        if job_id == mine_id:
            _hold(eraser, "agent_batch_jobs", mine_id)   # after the claim, before the item
        return real(db_, job, items_max, job_id, *claim)

    monkeypatch.setattr(batch, "_work_a_slice", erasure_starts)
    monkeypatch.setattr(threads, "LOCK_WAIT_SECONDS", 0.5)
    seen = {}

    def a_pass():
        batch.process_batch_jobs(db, limit=5)
        seen["theirs"] = db.get(AgentBatchJob, theirs.id).status
        seen["mine"] = db.get(AgentBatchJob, mine_id).status

    waited, _ = _without_waiting(a_pass, eraser)
    assert not waited, "the pass waited for the job the erasure holds"
    assert written == [other.id]
    assert seen == {"theirs": batch.STATUS_COMPLETED, "mine": batch.STATUS_PROCESSING}
    db.expire_all()
    job = db.get(AgentBatchJob, mine_id)
    # Held to the end of the pass: left 'processing' for the reaper, never
    # filed as failed, none of its items written.
    assert (job.status, job.imported, job.failed, job.last_error) == (
        batch.STATUS_PROCESSING, 0, 0, None), "the held job was filed as failed"


def test_the_vector_drain_leaves_a_row_an_erasure_holds_and_writes_the_next(
        db, tenant, other, eraser, monkeypatch):
    from korely_agent.services import vectors

    monkeypatch.setattr(threads, "LOCK_WAIT_SECONDS", 0.5)
    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
    monkeypatch.setattr(vectors, "_turns", {}, raising=False)
    held = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="a", content="held",
                       embedding_status="pending", created_at=NOW)
    free = AgentMemory(id=uuid.uuid4(), tenant_id=other.id, end_user_id="b", content="free",
                       embedding_status="pending", created_at=NOW + timedelta(seconds=1))
    db.add_all([held, free])
    db.commit()
    held_id, free_id = held.id, free.id

    def embed(text_, **kw):
        if text_ == "held":
            _hold(eraser, "agent_memories", held_id)     # the erasure starts during the call
        return [0.1] * EMBEDDING_DIM

    monkeypatch.setattr("korely_agent.core.llm.embed", embed)
    monkeypatch.setattr("korely_agent.services.memory_engine.embed", embed)

    def a_pass():
        vectors.process_pending_vectors(db, limit=10)
        return {db.get(AgentMemory, i).embedding_status for i in (held_id,)}

    waited, held_then = _without_waiting(a_pass, eraser)
    assert not waited, "the vector's write waited for the row the erasure holds"
    assert held_then == {"pending"}
    db.expire_all()
    assert db.get(AgentMemory, free_id).embedding_status == "done"
    assert db.get(AgentMemory, held_id).embedding_status == "pending"


class _NoneStart:
    """The webhook round's lanes with every thread taken: nothing starts,
    the whole claim goes back."""

    def share(self, slots, accounts, most=None):
        return slots

    def free(self, slots):
        return slots

    def running(self):
        return {}

    def wait_idle(self, seconds):
        return True

    def __init__(self, before):
        self.before = before

    def run(self, jobs, *, slots, share, patience, give_up):
        self.before()
        return {}, list(range(len(jobs)))


def test_the_webhook_round_leaves_a_claim_an_erasure_holds_and_gives_back_the_rest(
        db, tenant, other, eraser, monkeypatch):
    from korely_agent.db.models import AgentWebhookDelivery, AgentWebhookEndpoint
    from korely_agent.services import webhooks

    ids = {}
    for t in (tenant, other):
        e = AgentWebhookEndpoint(id=uuid.uuid4(), tenant_id=t.id, url="https://example.invalid/h",
                                 secret="whsec_x", events=["*"])
        db.add(e)
        db.flush()
        d = AgentWebhookDelivery(id=uuid.uuid4(), tenant_id=t.id, endpoint_id=e.id,
                                 event_type="memory.created", payload={}, status="pending",
                                 created_at=NOW)
        db.add(d)
        ids[t.id] = d.id
    db.commit()
    monkeypatch.setattr(webhooks, "_sending",
                        _NoneStart(lambda: _hold(eraser, "agent_webhook_deliveries", ids[tenant.id])))
    monkeypatch.setattr(webhooks, "_turns", threads.Rotation(), raising=False)
    _bounded(db)
    webhooks.process_deliveries(db, limit=20)
    eraser.rollback()
    db.expire_all()
    assert db.get(AgentWebhookDelivery, ids[other.id]).attempts == 0, "the free claim was not given back"
    assert db.get(AgentWebhookDelivery, ids[tenant.id]).attempts == 1, (
        "the held claim keeps its attempt and its lease")
