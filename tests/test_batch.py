"""Bulk import.

This is the first thing somebody evaluating the product does: point it at their
data and wait. So the two things that matter are that a bad row does not take
the job down with it, and that a job interrupted halfway does not import the
first half twice when it resumes.
"""
import uuid

import pytest

from korely_agent.db.models import AgentBatchJob, AgentMemory, Tenant
from korely_agent.services import batch



@pytest.fixture(autouse=True)
def no_extraction(monkeypatch):
    """Facts are mined elsewhere and have their own tests; here they are noise."""
    monkeypatch.setattr(
        "korely_agent.services.facts.extract_and_persist_facts",
        lambda *a, **k: [],
    )


def items(n, prefix="memory"):
    return [{"content": f"{prefix} {i}", "user_id": "u1"} for i in range(n)]


class TestEnqueueAndRead:
    def test_a_job_reports_what_it_received(self, db, tenant):
        job = batch.enqueue_batch(db, tenant.id, items(3))
        got = batch.job_status(db, tenant.id, job.id)
        assert got["status"] == "pending"
        assert got["received"] == 3
        assert got["imported"] == 0
        assert got["id"].startswith("job_")

    def test_another_tenant_cannot_read_the_job(self, db, tenant):
        other = Tenant(id=uuid.uuid4(), name="Other")
        db.add(other)
        db.commit()
        job = batch.enqueue_batch(db, tenant.id, items(1))
        assert batch.job_status(db, other.id, job.id) is None

    def test_an_unknown_job_is_none_not_an_exception(self, db, tenant):
        assert batch.job_status(db, tenant.id, uuid.uuid4()) is None


class TestOneBadRowDoesNotSinkTheJob:
    def test_the_good_ones_still_land(self, db, tenant):
        payload = [
            {"content": "good one", "user_id": "u1"},
            {"content": "   ", "user_id": "u1"},          # blank
            {"content": "good two", "user_id": "u1"},
            {"user_id": "u1"},                            # no content at all
        ]
        job = batch.enqueue_batch(db, tenant.id, payload)
        batch.process_batch_jobs(db, limit=5)

        got = batch.job_status(db, tenant.id, job.id)
        assert got["status"] == "completed"
        assert (got["imported"], got["failed"]) == (2, 2)
        assert {e["index"] for e in got["errors"]} == {1, 3}
        assert all("content" in e["error"] for e in got["errors"])

    def test_the_errors_say_which_row(self, db, tenant):
        job = batch.enqueue_batch(db, tenant.id, [
            {"content": "fine", "user_id": "u1"},
            {"content": "", "user_id": "u1"},
        ])
        batch.process_batch_jobs(db, limit=5)
        errs = batch.job_status(db, tenant.id, job.id)["errors"]
        assert errs == [{"index": 1, "error": "content must not be empty or whitespace"}]

    def test_every_error_reaches_the_row(self, db, tenant, engine):
        """Read as GET /v1/batch reads it, from another session: the worker's
        own session held the lists it had grown in place, and the row kept the
        first error at most (audit of 17 Oct 2026)."""
        from sqlalchemy.orm import Session

        job = batch.enqueue_batch(db, tenant.id, [
            {"content": "", "user_id": "u1"},
            {"content": "fine", "user_id": "u1"},
            {"content": "   ", "user_id": "u1"},
            {"content": "fine too", "user_id": "u1"},
        ])
        batch.process_batch_jobs(db, limit=5)
        other = Session(bind=engine)
        try:
            got = batch.job_status(other, tenant.id, job.id)
            ids = other.get(AgentBatchJob, job.id).created_memory_ids
        finally:
            other.close()
        assert (got["imported"], got["failed"]) == (2, 2)
        assert [e["index"] for e in got["errors"]] == [0, 2]
        assert len(ids) == 2

    def test_an_all_bad_job_still_completes(self, db, tenant):
        job = batch.enqueue_batch(db, tenant.id, [{"content": ""} for _ in range(3)])
        batch.process_batch_jobs(db, limit=5)
        got = batch.job_status(db, tenant.id, job.id)
        assert got["status"] == "completed"
        assert (got["imported"], got["failed"]) == (0, 3)


class TestResumingDoesNotImportTwice:
    def test_a_job_interrupted_halfway_carries_on(self, db, tenant, monkeypatch):
        """The failure mode this guards: a crash at row four re-importing rows
        one to three, so the customer's data arrives twice and nothing says so.
        """
        job = batch.enqueue_batch(db, tenant.id, items(6))

        real = batch._import_one
        calls = {"n": 0}

        def fail_at_four(db_, tenant_id, item, **kw):
            calls["n"] += 1
            if calls["n"] == 4:
                raise RuntimeError("the process died here")
            return real(db_, tenant_id, item, **kw)

        monkeypatch.setattr(batch, "_import_one", fail_at_four)
        batch.process_batch_jobs(db, limit=5)

        mid = batch.job_status(db, tenant.id, job.id)
        assert mid["status"] == "failed"
        assert mid["imported"] == 3

        # Second attempt, nothing in the way.
        monkeypatch.setattr(batch, "_import_one", real)
        db.query(AgentBatchJob).filter(AgentBatchJob.id == job.id).update(
            {"next_retry_at": None})
        db.commit()
        batch.process_batch_jobs(db, limit=5)

        done = batch.job_status(db, tenant.id, job.id)
        assert done["status"] == "completed"
        assert done["imported"] == 6

        contents = [m.content for m in db.query(AgentMemory)
                    .filter(AgentMemory.tenant_id == tenant.id).all()]
        assert len(contents) == 6, f"imported twice: {sorted(contents)}"
        assert len(set(contents)) == 6

    def test_a_job_that_keeps_failing_stops_being_picked_up(self, db, tenant,
                                                            monkeypatch):
        job = batch.enqueue_batch(db, tenant.id, items(2))
        monkeypatch.setattr(batch, "_import_one",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))

        for _ in range(10):
            db.query(AgentBatchJob).filter(AgentBatchJob.id == job.id).update(
                {"next_retry_at": None})
            db.commit()
            batch.process_batch_jobs(db, limit=5)

        db.refresh(job)
        assert job.attempts <= job.max_attempts
        assert batch.process_batch_jobs(db, limit=5) == {"processed": 0}


class ProcessDied(BaseException):
    """A kill, not an error: the drain's `except Exception` does not see it,
    exactly as it would not see the process vanish."""


class TestAJobAWorkerDiedOn:
    """Left 'processing' by a worker that died, a job was never picked again:
    the drain reads pending and failed only (round 13)."""

    def die_at_three(self, db, tenant, monkeypatch):
        job = batch.enqueue_batch(db, tenant.id, items(5))
        real = batch._import_one
        calls = {"n": 0}

        def import_one(db_, tenant_id, item, **kw):
            calls["n"] += 1
            if calls["n"] == 3:
                raise ProcessDied()
            return real(db_, tenant_id, item, **kw)

        monkeypatch.setattr(batch, "_import_one", import_one)
        with pytest.raises(ProcessDied):
            batch.process_batch_jobs(db, limit=5)
        db.rollback()
        monkeypatch.setattr(batch, "_import_one", real)
        return job

    def quiet_for(self, db, job, minutes):
        from datetime import datetime, timedelta, timezone

        db.query(AgentBatchJob).filter(AgentBatchJob.id == job.id).update(
            {"updated_at": datetime.now(timezone.utc) - timedelta(minutes=minutes)})
        db.commit()

    def test_it_resumes_where_it_stopped_and_imports_nothing_twice(self, db, tenant, monkeypatch):
        job = self.die_at_three(db, tenant, monkeypatch)
        assert batch.job_status(db, tenant.id, job.id)["status"] == "processing"
        self.quiet_for(db, job, 20)

        batch.process_batch_jobs(db, limit=5)

        done = batch.job_status(db, tenant.id, job.id)
        assert done["status"] == "completed" and done["imported"] == 5
        contents = [m.content for m in db.query(AgentMemory)
                    .filter(AgentMemory.tenant_id == tenant.id).all()]
        assert sorted(contents) == sorted(f"memory {i}" for i in range(5)), "imported twice or lost"

    def test_it_says_why_it_went_back(self, db, tenant, monkeypatch):
        job = self.die_at_three(db, tenant, monkeypatch)
        self.quiet_for(db, job, 20)

        assert batch.reclaim_interrupted(db) == 1
        db.refresh(job)
        assert job.status == "failed" and job.last_error == batch.INTERRUPTED
        assert job.attempts == 1, "the attempt the dead worker used still counts"

    def test_the_item_in_flight_when_it_died_is_not_written_twice(self, db, tenant, monkeypatch):
        """Killed after the third memory was committed and before the job
        counted it: the resumed job must find that memory, not write another."""
        from korely_agent.services import usage

        job = batch.enqueue_batch(db, tenant.id, items(5))
        calls = {"n": 0}
        real_log = usage.log_v1_request

        # The trail's row runs after the memory is committed, before the count
        # (the webhook did until the audit of 17 Oct 2026; it is enqueued in
        # the memory's own transaction now).
        def log(*a, **k):
            calls["n"] += 1
            if calls["n"] == 3:
                raise ProcessDied()
            return real_log(*a, **k)

        monkeypatch.setattr(usage, "log_v1_request", log)
        with pytest.raises(ProcessDied):
            batch.process_batch_jobs(db, limit=5)
        db.rollback()
        monkeypatch.setattr(usage, "log_v1_request", real_log)
        self.quiet_for(db, job, 20)

        batch.process_batch_jobs(db, limit=5)

        assert batch.job_status(db, tenant.id, job.id)["imported"] == 5
        contents = [m.content for m in db.query(AgentMemory)
                    .filter(AgentMemory.tenant_id == tenant.id).all()]
        assert sorted(contents) == sorted(f"memory {i}" for i in range(5)), "the third, twice"

    def test_a_job_somebody_is_still_working_is_left_alone(self, db, tenant, monkeypatch):
        job = self.die_at_three(db, tenant, monkeypatch)
        self.quiet_for(db, job, 1)

        assert batch.process_batch_jobs(db, limit=5) == {"processed": 0}
        db.refresh(job)
        assert job.status == "processing"


class TestIsolation:
    def test_a_drain_does_not_mix_two_tenants_payloads(self, db, tenant):
        other = Tenant(id=uuid.uuid4(), name="Other")
        db.add(other)
        db.commit()
        batch.enqueue_batch(db, tenant.id, [{"content": "mine", "user_id": "u1"}])
        batch.enqueue_batch(db, other.id, [{"content": "theirs", "user_id": "u1"}])

        batch.process_batch_jobs(db, limit=5)

        mine = db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant.id).all()
        theirs = db.query(AgentMemory).filter(AgentMemory.tenant_id == other.id).all()
        assert [m.content for m in mine] == ["mine"]
        assert [m.content for m in theirs] == ["theirs"]


class TestEmptyQueue:
    def test_nothing_to_do_is_not_an_error(self, db, tenant):
        assert batch.process_batch_jobs(db, limit=5) == {"processed": 0}


class TestEachAccountItsShare:
    """On the hosted service every customer is an account of one install
    (8 Oct 2026). Oldest first across the install, an account that queued
    many imports held every other account's import behind all of them."""

    def test_a_queue_of_imports_does_not_hold_another_account(self, db, tenant, monkeypatch):
        other = Tenant(id=uuid.uuid4(), name="Other")
        db.add(other)
        db.commit()
        for _ in range(10):
            batch.enqueue_batch(db, tenant.id, items(1))
        late = batch.enqueue_batch(db, other.id, items(1))
        seen = []
        monkeypatch.setattr(batch, "_import_one",
                            lambda db, tenant_id, item, **k: (seen.append(tenant_id), (None, "skipped"))[1])
        batch.process_batch_jobs(db, limit=5)
        assert other.id in seen
        assert seen.count(tenant.id) == 3
        db.refresh(late)
        assert late.status != batch.STATUS_PENDING

    def test_one_account_claims_as_before(self, db, tenant):
        from datetime import datetime, timezone

        for _ in range(10):
            batch.enqueue_batch(db, tenant.id, items(1))
        size, share, _ranked, _chosen = batch._claim_size_and_shares(db, 5, datetime.now(timezone.utc))
        assert (size, share) == (5, 5)  # the whole claim is the one account's
        batch.process_batch_jobs(db, limit=5)
        assert db.query(AgentBatchJob).filter(
            AgentBatchJob.status == batch.STATUS_COMPLETED).count() == 5


@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient

    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


class TestTheTrailNamesWhatWasImported:
    """POST /v1/batch left one `batch:create` row with no end user and no
    memory, so GET /v1/audit?user_id=ada said nothing of what was imported
    for Ada (audit of 17 Oct 2026). Each imported memory is a `memories:add`
    row now, as the single write's."""

    def test_an_access_request_finds_the_imported_memories(self, db, tenant, client):
        from korely_agent.services import keys as key_service
        from tests.conftest import seeded_project_id

        _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="importer",
                                                project_id=seeded_project_id(db, tenant.id))
        head = {"Authorization": f"Bearer {full}", "X-Korely-Client": "mem0-import"}
        r = client.post("/v1/batch", headers=head, json={"memories": [
            {"content": "Ada likes tea", "user_id": "ada"},
            {"content": "Bo likes coffee", "user_id": "bo"}]})
        assert r.status_code == 202, r.text
        batch.process_batch_jobs(db, limit=5)

        mine = client.get("/v1/memories", params={"user_id": "ada"}, headers=head).json()
        trail = client.get("/v1/audit", params={"user_id": "ada", "action": "write"},
                           headers=head).json()
        assert trail["total"] == 1, trail
        row = trail["events"][0]
        assert row["target_id"] == mine["memories"][0]["id"]
        assert row["actor"] == "worker"
        assert row["meta"]["batch"] == r.json()["id"] and row["meta"]["client"] == "mem0-import"
        assert client.get("/v1/audit", params={"user_id": "bo"}, headers=head).json()["total"] == 1

    def test_an_item_a_dead_attempt_wrote_is_not_logged_twice(self, db, tenant):
        from korely_agent.db.models import AuditEvent

        job = batch.enqueue_batch(db, tenant.id, [{"content": "Ada likes tea", "user_id": "ada"}])
        batch.process_batch_jobs(db, limit=5)
        db.query(AgentBatchJob).filter(AgentBatchJob.id == job.id).update(
            {"imported": 0, "status": batch.STATUS_FAILED, "next_retry_at": None})
        db.commit()
        batch.process_batch_jobs(db, limit=5)
        assert db.query(AuditEvent).filter(AuditEvent.end_user_id == "ada").count() == 1


class TestThePostAnswersTheStoredStatus:
    """POST /v1/batch answered 'processing' for a job stored 'pending', and
    GET then answered 'pending', a word the POST never named: a client
    polling until the status was no longer 'processing' stopped at once,
    with nothing imported (audit of 17 Oct 2026)."""

    def test_the_post_and_the_get_say_the_same_word(self, db, tenant, client):
        from korely_agent.services import keys as key_service
        from tests.conftest import seeded_project_id

        _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="importer",
                                                project_id=seeded_project_id(db, tenant.id))
        head = {"Authorization": f"Bearer {full}"}
        r = client.post("/v1/batch", headers=head, json={"memories": [
            {"content": "Ada likes tea", "user_id": "ada"}]})
        assert r.status_code == 202, r.text
        got = client.get(f"/v1/batch/{r.json()['id']}", headers=head).json()
        assert r.json()["status"] == got["status"] == batch.STATUS_PENDING
        batch.process_batch_jobs(db, limit=5)
        assert client.get(f"/v1/batch/{r.json()['id']}", headers=head).json()["status"] == \
            batch.STATUS_COMPLETED

    def test_the_contract_names_every_word(self, client):
        spec = client.get("/openapi.json").json()["components"]["schemas"]
        for model in ("BatchAccepted", "BatchStatus"):
            assert spec[model]["properties"]["status"]["enum"] == list(batch.STATUSES)


class TestAJobEndsWithItsKey:
    """A job of POST /v1/batch kept its account and its project, not its
    key: once the key was revoked, expired or disconnected as an OAuth app,
    every route refused it, and every job it had queued went on importing,
    into an archived project or, after a project's deletion, into the default
    one (audit of 18 Oct 2026). The worker now asks the key before each
    item."""

    def _queue(self, db, tenant, client, n=3, **key_kw):
        from korely_agent.services import keys as key_service
        from tests.conftest import seeded_project_id

        key_kw.setdefault("project_id", seeded_project_id(db, tenant.id))
        row, full = key_service.create_api_key(db, tenant_id=tenant.id,
                                               name=f"importer {uuid.uuid4().hex[:6]}", **key_kw)
        r = client.post("/v1/batch", headers={"Authorization": f"Bearer {full}"},
                        json={"memories": items(n)})
        assert r.status_code == 202, r.text
        from korely_agent.services.ids import parse_job_id

        job = db.get(AgentBatchJob, parse_job_id(r.json()["id"]))
        assert job.api_key_id == row.id
        return row, job

    def _ended(self, db, job, n=3):
        batch.process_batch_jobs(db, limit=5)
        db.expire_all()
        job = db.get(AgentBatchJob, job.id)
        assert job.status == batch.STATUS_COMPLETED
        assert (job.imported, job.failed) == (0, n)
        assert [e["error"] for e in job.errors] == [batch.KEY_ENDED] * n
        assert job.payload == [batch.DROPPED_ITEM] * n, "nothing kept of what was never imported"
        assert db.query(AgentMemory).count() == 0

    def test_a_revoked_key_imports_nothing_more(self, db, tenant, client):
        from korely_agent.services import keys as key_service

        row, job = self._queue(db, tenant, client)
        assert key_service.revoke_api_key(db, key_id=row.id, tenant_id=tenant.id)
        self._ended(db, job)

    def test_an_expired_key_imports_nothing_more(self, db, tenant, client):
        from datetime import datetime, timedelta, timezone

        row, job = self._queue(db, tenant, client)
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        self._ended(db, job)

    def test_a_key_moved_out_of_the_project_imports_nothing_more_there(self, db, tenant, client):
        from korely_agent.services import keys as key_service
        from korely_agent.services import projects as projects_service

        row, job = self._queue(db, tenant, client)
        elsewhere = projects_service.create_project(db, tenant_id=tenant.id, name="Elsewhere")
        assert key_service.move_key(db, key_id=row.id, tenant_id=tenant.id, project_id=elsewhere.id)
        self._ended(db, job)

    def test_an_oauth_app_s_hour_is_not_the_end_of_its_import(self, db, tenant, client):
        """An app's key expires every hour and is renewed on the same row at
        each refresh: its import goes on while the grant is live, and stops
        when the app is disconnected."""
        from datetime import datetime, timedelta, timezone

        from korely_agent.db.models import OAuthClient
        from korely_agent.services import oauth as oauth_service

        db.add(OAuthClient(client_id="app-1", info={"client_name": "ChatGPT"}))
        db.commit()
        row, job = self._queue(db, tenant, client, n=2, oauth_client_id="app-1",
                               expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                               expires_in_days=None)
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.commit()
        batch.process_batch_jobs(db, limit=5)
        db.expire_all()
        assert db.get(AgentBatchJob, job.id).imported == 2

        db.query(AgentMemory).delete()
        db.query(AgentBatchJob).delete()
        db.commit()
        row2, job2 = self._queue(db, tenant, client, n=2, oauth_client_id="app-1",
                                 expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                                 expires_in_days=None)
        oauth_service._revoke_family(db, tenant.id, row2.id, reason="revoked by the app")
        db.commit()
        self._ended(db, job2, n=2)

    def test_the_key_ending_midway_stops_the_rest(self, db, tenant, client, monkeypatch):
        """Asked before every item, not once at the claim: a key revoked while
        the job runs stops it at the next item, and what was imported stays."""
        from korely_agent.services import keys as key_service

        row, job = self._queue(db, tenant, client, n=4)
        real = batch._import_one
        done = []

        def one_then_revoke(db_, *a, **k):
            out = real(db_, *a, **k)
            done.append(1)
            if len(done) == 2:
                key_service.revoke_api_key(db_, key_id=row.id, tenant_id=tenant.id)
            return out

        monkeypatch.setattr(batch, "_import_one", one_then_revoke)
        batch.process_batch_jobs(db, limit=5)
        db.expire_all()
        job = db.get(AgentBatchJob, job.id)
        assert job.status == batch.STATUS_COMPLETED and (job.imported, job.failed) == (2, 2)
        assert [e["index"] for e in job.errors] == [2, 3]
        assert [i["content"] for i in job.payload[:2]] == ["memory 0", "memory 1"]
        assert job.payload[2:] == [batch.DROPPED_ITEM] * 2
        assert db.query(AgentMemory).count() == 2

    def test_a_job_queued_before_the_column_is_imported_as_before(self, db, tenant):
        job = batch.enqueue_batch(db, tenant.id, items(2))
        assert job.api_key_id is None
        batch.process_batch_jobs(db, limit=5)
        db.refresh(job)
        assert job.imported == 2

    def test_deleting_the_project_does_not_carry_an_app_s_import_into_the_default(
            self, db, tenant, client):
        """delete_project revokes an OAuth app's key (the person consented to
        that project only) and moves the project's jobs to the default: the
        app's job stops there instead of writing in the default."""
        from datetime import datetime, timedelta, timezone

        from korely_agent.db.models import OAuthClient
        from korely_agent.services import projects as projects_service

        projects_service.get_or_create_default_project(db, tenant.id)
        doomed = projects_service.create_project(db, tenant_id=tenant.id, name="Doomed")
        db.add(OAuthClient(client_id="app-2", info={"client_name": "Claude"}))
        db.commit()
        _row, job = self._queue(db, tenant, client, n=2, project_id=doomed.id,
                                oauth_client_id="app-2", expires_in_days=None,
                                expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
        projects_service.delete_project(db, tenant.id, doomed)
        self._ended(db, job, n=2)

    def test_an_archived_project_takes_no_more_of_an_app_s_import(self, db, tenant, client):
        """An archived project takes no more of a queued import. The archive
        itself now refuses a project whose connected app is between two
        refreshes (its key is live until revoked: services/projects.py,
        agent 45), so the archive comes after the app is disconnected here,
        and the worker's own check is what holds if an archive is ever
        reached another way (`archived_at` set by hand below)."""
        from datetime import datetime, timedelta, timezone

        from korely_agent.db.models import OAuthClient
        from korely_agent.services import projects as projects_service

        quiet = projects_service.create_project(db, tenant_id=tenant.id, name="Quiet")
        db.add(OAuthClient(client_id="app-3", info={"client_name": "ChatGPT"}))
        db.commit()
        row, job = self._queue(db, tenant, client, n=2, project_id=quiet.id,
                               oauth_client_id="app-3", expires_in_days=None,
                               expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.commit()
        with pytest.raises(projects_service.ProjectHasLiveKeys):
            projects_service.archive_project(db, quiet)
        db.rollback()
        quiet = db.get(type(quiet), quiet.id)
        quiet.archived_at = datetime.now(timezone.utc)
        db.commit()
        self._ended(db, job, n=2)


class TestAnImportsMemoriesArriveInTheOrderSent:
    """An open question of 5.12.0: where several memories are written in
    one transaction, `created_at` ties and the drain's order falls back to the
    id. The import writes one memory per transaction, so each has its own
    arrival time, and an account's jobs run in the order they were queued,
    interleaved item by item with other accounts' only."""

    def test_each_memory_has_its_own_arrival_in_the_order_sent(self, db, tenant):
        batch.enqueue_batch(db, tenant.id, items(12, "first"))
        batch.enqueue_batch(db, tenant.id, items(12, "second"))
        other = Tenant(id=uuid.uuid4(), name="Other")
        db.add(other)
        db.commit()
        batch.enqueue_batch(db, other.id, items(7, "other"))
        while batch.process_batch_jobs(db, limit=5).get("more"):
            pass
        rows = (db.query(AgentMemory.content, AgentMemory.created_at)
                .filter(AgentMemory.tenant_id == tenant.id)
                .order_by(AgentMemory.created_at, AgentMemory.id).all())
        assert [c for c, _t in rows] == ([f"first {i}" for i in range(12)]
                                         + [f"second {i}" for i in range(12)])
        assert len({t for _c, t in rows}) == len(rows), "two memories share an arrival time"


class TestABadRowFailsAsThatRow:
    def test_a_row_the_reader_cannot_take_is_its_own_error_not_the_jobs(self, db, tenant,
                                                                       monkeypatch):
        """Audit of 27 Oct 2026: metadata nested past the JSON reader's depth, stored
        before the door refused it, raised RecursionError in the worker and
        failed the whole job, the good rows with it. The reader is made to
        raise it for that row (a payload that deep cannot be enqueued now)."""
        from korely_agent.services import memory_write

        real = memory_write.checked

        def checked(content, **kw):
            if content == "bad one":
                raise RecursionError("maximum recursion depth exceeded")
            return real(content, **kw)

        monkeypatch.setattr(memory_write, "checked", checked)
        job = batch.enqueue_batch(db, tenant.id, [
            {"content": "good one", "user_id": "u1"},
            {"content": "bad one", "user_id": "u1"},
            {"content": "good two", "user_id": "u1"}])
        batch.process_batch_jobs(db, limit=5)
        db.expire_all()
        job = db.get(AgentBatchJob, job.id)
        assert job.status == batch.STATUS_COMPLETED
        assert (job.imported, job.failed) == (2, 1)
        assert [e["index"] for e in job.errors] == [1]


class TestOneRoundPerPass:
    """Review of C2: a pass ran every claimed job to its end. A job then
    committed only every (accounts - 1) x 5 items, long enough for another
    replica to take it for dead, and an account ready just after the claim
    waited behind all of it. A pass is one round now."""

    def test_a_pass_writes_one_turn_and_gives_the_job_back_without_an_attempt(self, db, tenant):
        job = batch.enqueue_batch(db, tenant.id, items(12))
        out = batch.process_batch_jobs(db, limit=5)
        assert out["more"] is True
        db.expire_all()
        job = db.get(AgentBatchJob, job.id)
        assert (job.imported, job.status, job.attempts) == (batch.ITEMS_PER_TURN, "pending", 0)
        while batch.process_batch_jobs(db, limit=5).get("more"):
            pass
        db.expire_all()
        job = db.get(AgentBatchJob, job.id)
        assert (job.imported, job.status) == (12, batch.STATUS_COMPLETED)
        assert job.attempts == 1

    def test_an_account_ready_after_the_claim_waits_one_round(self, db, tenant, monkeypatch):
        other = Tenant(id=uuid.uuid4(), name="Other")
        db.add(other)
        db.commit()
        batch.enqueue_batch(db, tenant.id, items(500, "big"))
        seen = []
        real = batch._import_one
        monkeypatch.setattr(batch, "_import_one",
                            lambda db_, t, item, **k: (seen.append(t), real(db_, t, item, **k))[1])
        batch.process_batch_jobs(db, limit=5)
        batch.enqueue_batch(db, other.id, items(1, "small"))
        batch.process_batch_jobs(db, limit=5)
        assert other.id in seen and len(seen) <= 2 * batch.ITEMS_PER_TURN + 1

    def test_a_job_in_a_round_is_not_taken_for_dead_by_another_replica(self, db, tenant, monkeypatch):
        """A short stale window: a job given back between rounds is pending,
        never 'processing' with an old updated_at, so the reaper leaves it."""
        monkeypatch.setenv("DRAIN_STALE_MINUTES", "1")
        from korely_agent.config import get_settings

        get_settings.cache_clear()
        try:
            job = batch.enqueue_batch(db, tenant.id, items(12))
            batch.process_batch_jobs(db, limit=5)
            assert batch.reclaim_interrupted(db) == 0
            db.expire_all()
            assert db.get(AgentBatchJob, job.id).last_error is None
        finally:
            get_settings.cache_clear()

    def test_a_fault_of_ours_stays_a_retried_job_not_a_bad_row(self, db, tenant, monkeypatch):
        """Only the reader's own error is the row's: a ValueError from the
        write is a defect of the code and the job is retried."""
        from korely_agent.services import memory_write

        def broken(*a, **k):
            raise ValueError("a bug")

        monkeypatch.setattr(memory_write, "store", broken)
        job = batch.enqueue_batch(db, tenant.id, items(2))
        batch.process_batch_jobs(db, limit=5)
        db.expire_all()
        job = db.get(AgentBatchJob, job.id)
        assert job.status == batch.STATUS_FAILED and job.failed == 0
