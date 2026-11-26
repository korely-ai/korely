"""A restart gives back the /v1/batch jobs it was importing (1.0.40).

A job being imported when its process ended (a deploy, a crash) stayed
'processing' until the reaper's clock gave it back, `drain_stale_minutes`
later (15 by default): the import of whoever migrated during a deploy stood
still for those minutes (code review finding of 13 Nov 2026). The memories'
claims have been told apart by the key of the process that holds them since
1.0.22 (services/holder.py, tests/test_a_restart_gives_its_claims_back.py,
which proves the key's liveness with a killed process); a job's claim now
carries the same key (`agent_batch_jobs.claimed_by`).
"""
import pytest
from sqlalchemy import text

from korely_agent.db.models import AgentBatchJob
from korely_agent.services import batch
from korely_agent.services import holder as holders
from korely_agent.services.facts import drain as facts_drain
from tests.test_batch import ProcessDied, items


def _held(db, key) -> bool:
    return bool(db.execute(text(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
        " AND objsubid = 2 AND classid = :space AND objid = :key"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"),
        {"space": holders.SPACE, "key": key}).scalar())


@pytest.fixture
def an_ended_key(db):
    """A key no session of the database holds: a process's that has ended."""
    key = 2**31 - 7
    assert not _held(db, key)
    db.rollback()
    return key


def _left_processing(db, tenant, key):
    """A job a process left mid-import: 'processing', stamped `key`, and
    updated a moment ago, so the clock alone would wait for it."""
    job = batch.enqueue_batch(db, tenant.id, items(3))
    db.query(AgentBatchJob).filter(AgentBatchJob.id == job.id).update(
        {AgentBatchJob.status: batch.STATUS_PROCESSING, AgentBatchJob.attempts: 1,
         AgentBatchJob.claimed_by: key}, synchronize_session=False)
    db.commit()
    return job.id


def _job(db, job_id):
    db.expire_all()
    return db.get(AgentBatchJob, job_id)


def test_a_job_of_a_process_that_ended_goes_back_at_once(db, tenant, an_ended_key):
    job_id = _left_processing(db, tenant, an_ended_key)
    mine = facts_drain.this_process_key(db)
    assert mine is not None and mine != an_ended_key

    assert batch.reclaim_interrupted(db, holding=mine) == 1
    job = _job(db, job_id)
    assert (job.status, job.last_error) == (batch.STATUS_FAILED, batch.INTERRUPTED)


def test_the_next_pass_imports_it_without_waiting_for_the_clock(db, tenant, an_ended_key):
    job_id = _left_processing(db, tenant, an_ended_key)
    # The pass gives it back and claims it again in the same call; a failed
    # job is ready at once (no `next_retry_at`).
    batch.process_batch_jobs(db, limit=5)
    batch.process_batch_jobs(db, limit=5)
    job = _job(db, job_id)
    assert (job.status, job.imported) == (batch.STATUS_COMPLETED, 3)


def test_a_job_a_live_process_holds_is_left(db, engine, tenant):
    """Its key held by a session (another process's lock): not given back,
    however the reaper is asked."""
    with engine.connect() as theirs:
        key = 2**31 - 11
        assert theirs.execute(text("SELECT pg_try_advisory_lock(:s, :k)"),
                              {"s": holders.SPACE, "k": key}).scalar()
        theirs.commit()
        try:
            job_id = _left_processing(db, tenant, key)
            assert batch.reclaim_interrupted(db, holding=facts_drain.this_process_key(db)) == 0
            batch.process_batch_jobs(db, limit=5)
            job = _job(db, job_id)
            assert (job.status, job.claimed_by, job.imported) == (
                batch.STATUS_PROCESSING, key, 0)
        finally:
            theirs.execute(text("SELECT pg_advisory_unlock(:s, :k)"),
                           {"s": holders.SPACE, "k": key})
            theirs.commit()


def test_a_process_without_its_key_gives_back_only_by_the_clock(db, tenant, an_ended_key):
    """A process whose own lock session was cut reads as ended until it takes
    its key again: holding none, it cannot tell its own jobs from an ended
    process's, and leaves them to the clock."""
    job_id = _left_processing(db, tenant, an_ended_key)
    assert batch.reclaim_interrupted(db, holding=None) == 0
    assert _job(db, job_id).status == batch.STATUS_PROCESSING


def test_an_unstamped_job_waits_for_the_clock(db, tenant):
    """A job claimed by a release before this one, or with no key: as before."""
    job_id = _left_processing(db, tenant, None)
    assert batch.reclaim_interrupted(db, holding=facts_drain.this_process_key(db)) == 0
    assert _job(db, job_id).status == batch.STATUS_PROCESSING


def test_the_claim_is_stamped_with_this_processs_key(db, tenant, monkeypatch):
    job = batch.enqueue_batch(db, tenant.id, items(3))
    real = batch._import_one

    def dies_on_the_second(db_, tenant_id, item, **kw):
        if item.get("content", "").endswith("1"):
            raise ProcessDied()
        return real(db_, tenant_id, item, **kw)

    monkeypatch.setattr(batch, "_import_one", dies_on_the_second)
    with pytest.raises(ProcessDied):
        batch.process_batch_jobs(db, limit=5)
    db.rollback()
    left = _job(db, job.id)
    assert left.status == batch.STATUS_PROCESSING
    assert left.claimed_by == facts_drain.this_process_key(db) is not None


def test_a_pass_writes_nothing_on_a_job_another_pass_claimed_since(db, tenant, monkeypatch):
    """Given back while its lock session was cut and claimed by another pass,
    the job is that pass's: this one stops at its next item, counts nothing
    more on it and does not put it back in the queue."""
    job_id = batch.enqueue_batch(db, tenant.id, items(4)).id
    other = 2**31 - 13
    real = batch._import_one
    calls = {"n": 0}

    def another_pass_claims_it(db_, tenant_id, item, **kw):
        out = real(db_, tenant_id, item, **kw)
        calls["n"] += 1
        if calls["n"] == 1:
            db_.execute(text("UPDATE agent_batch_jobs SET claimed_by = :k WHERE id = :id"),
                        {"k": other, "id": job_id})
        return out

    monkeypatch.setattr(batch, "_import_one", another_pass_claims_it)
    batch.process_batch_jobs(db, limit=5)
    after = _job(db, job_id)
    assert calls["n"] == 1, "the pass went on importing another pass's job"
    assert (after.status, after.claimed_by, after.imported) == (
        batch.STATUS_PROCESSING, other, 1)


def _another_pass_claims_it_at(monkeypatch, job_id, at: int, other: int):
    """The import of the `at`-th item of this pass changes the job's stamp to
    `other`, as another pass's claim would after a give-back."""
    real = batch._import_one
    calls = {"n": 0}

    def import_one(db_, tenant_id, item, **kw):
        out = real(db_, tenant_id, item, **kw)
        calls["n"] += 1
        if calls["n"] == at:
            db_.execute(text("UPDATE agent_batch_jobs SET claimed_by = :k WHERE id = :id"),
                        {"k": other, "id": job_id})
        return out

    monkeypatch.setattr(batch, "_import_one", import_one)
    return calls


def test_the_round_does_not_put_back_a_job_another_pass_claimed(db, tenant, monkeypatch):
    """A job with items left at the end of its turn goes back to the queue
    (`_give_back`) only while it is still this pass's claim: one another pass
    claimed meanwhile stays 'processing', that pass's."""
    job_id = batch.enqueue_batch(db, tenant.id, items(batch.ITEMS_PER_TURN + 3)).id
    other = 2**31 - 17
    calls = _another_pass_claims_it_at(monkeypatch, job_id, batch.ITEMS_PER_TURN, other)
    batch.process_batch_jobs(db, limit=5)
    after = _job(db, job_id)
    assert calls["n"] == batch.ITEMS_PER_TURN
    assert (after.status, after.claimed_by) == (batch.STATUS_PROCESSING, other)


def test_a_pass_never_writes_completed_on_a_job_another_pass_claimed(db, tenant, monkeypatch):
    job_id = batch.enqueue_batch(db, tenant.id, items(2)).id
    other = 2**31 - 19
    _another_pass_claims_it_at(monkeypatch, job_id, 2, other)
    batch.process_batch_jobs(db, limit=5)
    after = _job(db, job_id)
    assert (after.status, after.claimed_by, after.completed_at) == (
        batch.STATUS_PROCESSING, other, None)


def test_a_pass_never_writes_failed_on_a_job_another_pass_claimed(db, tenant, monkeypatch):
    job_id = batch.enqueue_batch(db, tenant.id, items(3)).id
    other = 2**31 - 23
    real = batch._import_one
    calls = {"n": 0}

    def claimed_then_broken(db_, tenant_id, item, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            # Another pass claims it (committed: this pass holds the row's
            # lock for the item, so the change is made under it), then this
            # item breaks.
            db_.execute(text("UPDATE agent_batch_jobs SET claimed_by = :k WHERE id = :id"),
                        {"k": other, "id": job_id})
            db_.commit()
            raise RuntimeError("a fault of ours")
        return real(db_, tenant_id, item, **kw)

    monkeypatch.setattr(batch, "_import_one", claimed_then_broken)
    batch.process_batch_jobs(db, limit=5)
    after = _job(db, job_id)
    assert (after.status, after.claimed_by, after.last_error) == (
        batch.STATUS_PROCESSING, other, None)
