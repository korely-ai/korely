"""A /v1/batch payload is a copy of the memories it imports, and it is treated
as one.

Three things were missing:

* erasing an end user (DELETE /v1/users/{id}/memories) deleted their memories
  and facts and left their text in every batch job that had carried it,
  queued or finished: `agent_batch_jobs.payload`, text and metadata, readable
  in the table after the erasure said it was done;
* a job still queued, or one running, imported the erased end user again
  after the erasure, from that copy;
* finished jobs were never removed, so every payload ever sent stayed in the
  table for good.

Now an erasure scrubs that end user's items in the payloads it can reach (the
same project wall as the erasure), leaving a marker in place so the positions
a job resumes from still add up; the worker reads each item as it is when it
imports it, so an erasure that lands while a job runs is honoured by the rest
of the job; and the worker removes finished jobs after
`batch.FINISHED_JOB_RETENTION`. Deleting an agent namespace scrubs its items
the same way, since a queued job would bring the namespace back.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from tests.conftest import seeded_project_id
from korely_agent.db.models import AgentBatchJob, AgentMemory
from korely_agent.services import batch
from korely_agent.services import facts as agent_facts
from korely_agent.services import projects as project_service


MARIA = {"content": "Maria's card ends in 4242.", "user_id": "maria",
         "metadata": {"phone": "+39 333 1234567"}}
BOB = {"content": "Bob prefers email.", "user_id": "bob"}


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    """Facts are mined elsewhere and have their own tests; here they are noise."""
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                        lambda *a, **k: [])


@pytest.fixture
def pid(db, tenant):
    return seeded_project_id(db, tenant.id)


@pytest.fixture
def other_session(engine):
    """A second connection, as a request arriving while the worker runs."""
    s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def _payload(db, job_id):
    db.expire_all()
    return db.get(AgentBatchJob, job_id).payload


def _contents(db, tenant):
    db.expire_all()
    return sorted(m.content for m in db.query(AgentMemory)
                  .filter(AgentMemory.tenant_id == tenant.id).all())


class TestErasingAnEndUserErasesTheirCopies:
    def test_queued_and_finished_payloads_lose_their_text(self, db, tenant, pid):
        finished = batch.enqueue_batch(db, tenant.id, [dict(MARIA), dict(BOB)], project_id=pid)
        batch.process_batch_jobs(db, limit=5)
        queued = batch.enqueue_batch(db, tenant.id, [dict(BOB), dict(MARIA)], project_id=pid)
        assert db.get(AgentBatchJob, finished.id).status == "completed"

        agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)

        for job in (finished, queued):
            text = json.dumps(_payload(db, job.id))
            assert "4242" not in text and "333" not in text and "maria" not in text, (
                f"the erased end user's text is still in a batch payload: {text}")
            assert BOB["content"] in text, "another end user's item was touched"
        # Positions kept: a job resumes from `imported + failed`.
        assert len(_payload(db, queued.id)) == 2
        assert _payload(db, queued.id)[1] == batch.ERASED_ITEM

    def test_a_queued_job_does_not_bring_them_back(self, db, tenant, pid):
        job = batch.enqueue_batch(db, tenant.id, [dict(MARIA), dict(BOB)], project_id=pid)
        agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)

        batch.process_batch_jobs(db, limit=5)

        assert _contents(db, tenant) == [BOB["content"]]
        got = batch.job_status(db, tenant.id, job.id)
        assert (got["status"], got["imported"], got["failed"]) == ("completed", 1, 1)
        assert got["errors"] == [{"index": 0, "error": batch.ERASED_BEFORE_IMPORT}]

    def test_nor_does_a_job_already_running(self, db, tenant, pid, other_session, monkeypatch):
        """The erasure lands between two items of a job the worker holds: the
        rest of the job reads the payload as it is now, not as it was claimed."""
        job = batch.enqueue_batch(db, tenant.id, [dict(BOB), dict(MARIA)], project_id=pid)
        real = batch._import_one

        def then_erase(db_, tenant_id, item, **kw):
            out = real(db_, tenant_id, item, **kw)
            if item.get("user_id") == "bob":
                agent_facts.forget_end_user(other_session, tenant.id, "maria", project_id=pid)
            return out

        monkeypatch.setattr(batch, "_import_one", then_erase)
        batch.process_batch_jobs(db, limit=5)

        assert _contents(db, tenant) == [BOB["content"]], (
            "the worker imported the erased end user from the payload it had claimed")
        assert batch.job_status(db, tenant.id, job.id)["failed"] == 1

    @pytest.mark.parametrize("erase", ["user", "agent"])
    def test_nor_one_that_lands_during_the_item_itself(self, db, tenant, pid, other_session,
                                                      monkeypatch, erase):
        """The erasure lands while the worker computes the vector of that very
        item, before its memory is written (audit of 17 Oct 2026): it waits for
        the item to be written and then erases it, or the item, read again
        under the job's lock, is already erased."""
        import threading

        from korely_agent.services import memory_engine

        item = {**MARIA, "agent_id": "support"}
        job = batch.enqueue_batch(db, tenant.id, [item], project_id=pid)
        done = {}

        def erasure():
            if erase == "user":
                done["out"] = agent_facts.forget_end_user(other_session, tenant.id, "maria",
                                                          project_id=pid)
            else:
                done["out"] = agent_facts.delete_agent(other_session, tenant.id, "support",
                                                       project_id=pid)

        runner = threading.Thread(target=erasure)

        def the_vector_while_erasing(text):
            runner.start()
            runner.join(timeout=1.0)  # with the lock held, it waits for us
            return None

        monkeypatch.setattr(memory_engine, "memory_vector", the_vector_while_erasing)
        batch.process_batch_jobs(db, limit=5)
        runner.join(timeout=10)
        assert not runner.is_alive()
        assert done["out"]["memories_deleted"] == 1
        assert _contents(db, tenant) == [], "an erased end user's memory outlived the erasure"
        assert _payload(db, job.id) == [batch.ERASED_ITEM]

    def test_another_projects_jobs_are_not_touched(self, db, tenant, pid):
        other = project_service.create_project(db, tenant_id=tenant.id, name="Other")
        theirs = batch.enqueue_batch(db, tenant.id, [dict(MARIA)], project_id=other.id)

        agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)

        assert _payload(db, theirs.id) == [MARIA], (
            "an erasure through one project's key reached another project's data")

    def test_the_error_text_of_a_job_goes_with_it(self, db, tenant, pid):
        """`last_error` is the text of an exception, and a database error
        quotes the parameters of the statement that failed: the item's text."""
        job = batch.enqueue_batch(db, tenant.id, [dict(MARIA)], project_id=pid)
        db.query(AgentBatchJob).filter(AgentBatchJob.id == job.id).update(
            {"status": "failed", "last_error": "[parameters: (\"Maria's card ends in 4242.\")]"})
        db.commit()

        agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)

        db.expire_all()
        assert db.get(AgentBatchJob, job.id).last_error is None


class TestTheScrubHoldsOneJobAtATime:
    """Every job of the project, payload and all, came back in one list on
    every erasure, even of a memory id that answers 404: some 24 MB a stored
    job at its largest (audit of 19 Oct 2026)."""

    def test_one_payload_is_in_memory_at_a_time(self, db, tenant, pid):
        jobs = [batch.enqueue_batch(db, tenant.id, [dict(BOB), dict(MARIA)], project_id=pid)
                for _ in range(4)]
        db.commit()
        db.expunge_all()
        held = []

        def hit_at(job, i, item):
            held.append(sum(isinstance(o, AgentBatchJob) for o in db.identity_map.values()))
            return isinstance(item, dict) and item.get("user_id") == "maria"

        assert batch._scrub(db, tenant.id, pid, hit_at) == 4
        db.commit()
        assert max(held) == 1, held
        for job in jobs:
            assert json.dumps(_payload(db, job.id)).count("4242") == 0


class TestDeletingAnAgentNamespace:
    def test_its_items_go_too(self, db, tenant, pid):
        job = batch.enqueue_batch(db, tenant.id, [
            dict(BOB, agent_id="typo-bot"), dict(BOB, agent_id="support")], project_id=pid)
        agent_facts.delete_agent(db, tenant.id, "typo-bot", project_id=pid)

        assert _payload(db, job.id) == [batch.ERASED_ITEM, dict(BOB, agent_id="support")]
        batch.process_batch_jobs(db, limit=5)
        db.expire_all()
        assert [m.agent_id for m in db.query(AgentMemory).all()] == ["support"], (
            "a queued job brought the deleted namespace back")


class TestAProjectErasedWhileItsJobRuns:
    def test_the_job_stops_and_the_worker_does_not_fail(
            self, db, tenant, other_session, monkeypatch):
        """Erasing a project deletes its jobs (tests/test_project_isolation.py).
        A job the worker was in the middle of stops at the next item rather
        than importing the rest into a project that no longer exists."""
        project_service.get_or_create_default_project(db, tenant.id)
        gamma = project_service.create_project(db, tenant_id=tenant.id, name="Gamma")
        batch.enqueue_batch(db, tenant.id, [dict(BOB), dict(MARIA)], project_id=gamma.id)
        real = batch._import_one

        def then_erase(db_, tenant_id, item, **kw):
            out = real(db_, tenant_id, item, **kw)
            if item.get("user_id") == "bob":
                g = project_service.get_project(other_session, tenant.id, gamma.id)
                project_service.delete_project(other_session, tenant.id, g, erase=True)
            return out

        monkeypatch.setattr(batch, "_import_one", then_erase)
        batch.process_batch_jobs(db, limit=5)  # must not raise

        assert _contents(db, tenant) == []
        assert db.query(AgentBatchJob).count() == 0

    @pytest.mark.parametrize("erase", [True, False])
    def test_nor_during_the_item_itself(self, db, tenant, other_session, monkeypatch, erase):
        """The project goes while the worker computes the vector of one of its
        items (audit of 18 Oct 2026): the deletion takes the jobs' locks first,
        so it waits for the item's memory and erases it, or moves it, and
        nothing is filed in the default project with its end user in clear,
        or left with no project."""
        import threading

        from korely_agent.db.models import AgentForgetAudit
        from korely_agent.services import memory_engine

        default = project_service.get_or_create_default_project(db, tenant.id)
        gamma = project_service.create_project(db, tenant_id=tenant.id, name="Gamma")
        batch.enqueue_batch(db, tenant.id, [dict(MARIA), dict(BOB)], project_id=gamma.id)
        done = {}

        def deletion():
            g = project_service.get_project(other_session, tenant.id, gamma.id)
            done["out"] = project_service.delete_project(other_session, tenant.id, g, erase=erase)

        runner = threading.Thread(target=deletion)

        def the_vector_while_deleting(text):
            if not runner.is_alive() and "out" not in done:
                runner.start()
                runner.join(timeout=1.0)  # with the job held, it waits for us
            return None

        monkeypatch.setattr(memory_engine, "memory_vector", the_vector_while_deleting)
        batch.process_batch_jobs(db, limit=5)  # must not raise
        runner.join(timeout=10)
        assert not runner.is_alive()
        db.expire_all()
        left = db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant.id).all()
        if erase:
            assert left == [], "a memory of the erased project outlived the erasure"
            assert done["out"]["memories_deleted"] == 1
            record = db.query(AgentForgetAudit).filter(AgentForgetAudit.scope == "project").one()
            assert record.memories_forgotten == 1, "the erasure record counts it"
        else:
            # Moved, as everything the project held, and never left with no
            # project, where no key reads it.
            assert [m.project_id for m in left] == [default.id]
            assert done["out"]["memories"] == 1
        assert db.query(AgentBatchJob).filter(AgentBatchJob.project_id.is_(None)).count() == 0


class TestFinishedJobsAreRemoved:
    def _job(self, db, tenant, pid, *, status, attempts=1, age_days=0.0):
        job = batch.enqueue_batch(db, tenant.id, [dict(BOB)], project_id=pid)
        when = datetime.now(timezone.utc) - timedelta(days=age_days)
        db.query(AgentBatchJob).filter(AgentBatchJob.id == job.id).update({
            "status": status, "attempts": attempts,
            "created_at": when, "updated_at": when,
            "completed_at": when if status == "completed" else None,
        })
        db.commit()
        return job.id

    def test_past_the_window_and_finished_only(self, db, tenant, pid):
        past = batch.FINISHED_JOB_RETENTION.days + 1
        old_done = self._job(db, tenant, pid, status="completed", age_days=past)
        old_gave_up = self._job(db, tenant, pid, status="failed", attempts=3, age_days=past)
        # Still live, however old: a failure with attempts left, a queued job,
        # one the worker holds.
        old_retrying = self._job(db, tenant, pid, status="failed", attempts=1, age_days=past)
        old_queued = self._job(db, tenant, pid, status="pending", attempts=0, age_days=past)
        old_running = self._job(db, tenant, pid, status="processing", age_days=past)
        # Finished, inside the window: the caller may still be polling it.
        recent_done = self._job(db, tenant, pid, status="completed", age_days=1)

        assert batch.purge_finished(db) == 2

        db.expire_all()
        left = {j.id for j in db.query(AgentBatchJob).all()}
        assert left == {old_retrying, old_queued, old_running, recent_done}
        assert not left & {old_done, old_gave_up}

    def test_the_worker_does_it_on_its_hourly_pass(self, db, tenant, pid):
        from korely_agent import worker

        past = batch.FINISHED_JOB_RETENTION.days + 1
        self._job(db, tenant, pid, status="completed", age_days=past)
        worker._purge(db)
        db.expire_all()
        assert db.query(AgentBatchJob).count() == 0


@pytest.mark.parametrize("erase", ["user", "agent"])
def test_the_erasure_counts_the_facts_a_write_saved_while_it_waited(db, tenant, pid, other_session,
                                                                    erase):
    """A write of the same person holds its memory and saves a fact while the
    erasure runs: the cascade took that fact uncounted, and facts_deleted, the
    erasure record's number, said 0 (audit of 17 Oct 2026)."""
    import threading
    import time
    import uuid as _uuid
    from datetime import datetime, timezone

    from korely_agent.db.models import AgentFact

    m = AgentMemory(id=_uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                    agent_id="support", content="Maria likes tea.", graph_sync_status="processing")
    db.add(m)
    db.commit()
    other_session.query(AgentMemory).filter(AgentMemory.id == m.id).with_for_update().one()
    other_session.add(AgentFact(id=_uuid.uuid4(), tenant_id=tenant.id, project_id=pid,
                                end_user_id="maria", agent_id="support", source_memory_id=m.id,
                                subject_label="maria", predicate_raw="likes",
                                predicate_canonical="likes", object_label="tea",
                                valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    other_session.flush()
    writer = threading.Thread(target=lambda: (time.sleep(0.5), other_session.commit()))
    writer.start()
    if erase == "user":
        out = agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)
    else:
        out = agent_facts.delete_agent(db, tenant.id, "support", project_id=pid)
    writer.join()
    assert out["facts_deleted"] == 1 and out["memories_deleted"] == 1
