"""An account's API keys are capped and listed a page at a time (audit of 27
Oct 2026): GET /admin/keys read every key
of the account, and an account could make as many as it liked."""
import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentApiKey
from korely_agent.services import keys as key_service


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin(db, tenant):
    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="admin",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    return {"Authorization": f"Bearer {full}"}


def _fill(db, tenant, n):
    """n more keys, straight into the table (the cap is on the door)."""
    for i in range(n):
        key_service.create_api_key(db, tenant_id=tenant.id, name=f"bulk-{i}")


def test_a_ceiling_of_keys_and_room_again_after_a_revoke(db, tenant, client, admin, monkeypatch):
    monkeypatch.setattr(key_service, "MAX_API_KEYS", 4)
    _fill(db, tenant, 3)                       # the admin key and three: four
    r = client.post("/admin/keys", headers=admin, json={"name": "one-too-many"})
    assert r.status_code == 409 and r.json()["code"] == "too_many_keys"
    first = db.query(AgentApiKey).filter(AgentApiKey.name == "bulk-0").one()
    assert client.post(f"/admin/keys/{first.id}/revoke", headers=admin).status_code in (200, 204)
    assert client.post("/admin/keys", headers=admin, json={"name": "fits-now"}).status_code == 201


def test_a_connected_apps_key_is_not_counted(db, tenant, monkeypatch):
    monkeypatch.setattr(key_service, "MAX_API_KEYS", 1)
    from korely_agent.db.models import OAuthClient

    db.add(OAuthClient(client_id="c1", info={}))
    db.commit()
    key_service.create_api_key(db, tenant_id=tenant.id, name="mine")
    key_service.create_api_key(db, tenant_id=tenant.id, name="app", oauth_client_id="c1")
    with pytest.raises(key_service.TooManyKeys):
        key_service.create_api_key(db, tenant_id=tenant.id, name="another")


def test_the_list_is_a_page_with_the_live_keys_first(db, tenant, client, admin):
    _fill(db, tenant, 5)
    old = db.query(AgentApiKey).filter(AgentApiKey.name == "bulk-0").one()
    client.post(f"/admin/keys/{old.id}/revoke", headers=admin)
    names = [k["name"] for k in client.get("/admin/keys", headers=admin).json()]
    assert names[-1] == "bulk-0" and len(names) == 6
    page = client.get("/admin/keys?limit=2&offset=1", headers=admin).json()
    assert [k["name"] for k in page] == names[1:3]
    assert client.get("/admin/keys?limit=0", headers=admin).status_code == 422


# ── the revoked keys (audit of 27 Oct 2026, a known issue until 1.0.40) ─────
# They were never deleted, and only the live ones were counted: creating and
# revoking keys grew without end the rows every walk of an account's keys
# reads. Now they count, within the trail's retention, and leave after it.


def _revoked(db, tenant, n, *, days_ago=0):
    from datetime import datetime, timedelta, timezone

    for i in range(n):
        row, _full = key_service.create_api_key(db, tenant_id=tenant.id, name=f"gone-{days_ago}-{i}")
        row.revoked_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.commit()


def test_the_revoked_keys_count_within_the_retention(db, tenant, client, admin, monkeypatch):
    monkeypatch.setattr(key_service, "MAX_KEY_ROWS", 5)
    _revoked(db, tenant, 4)                    # the admin key and four revoked: five rows
    r = client.post("/admin/keys", headers=admin, json={"name": "one-too-many"})
    assert r.status_code == 409 and r.json()["code"] == "too_many_keys"
    assert "revoked" in r.json()["message"] and "400 days" in r.json()["message"]


def test_a_key_revoked_before_the_retention_leaves_the_count_and_the_table(db, tenant, monkeypatch):
    from korely_agent.db.models import AuditEvent

    monkeypatch.setattr(key_service, "MAX_KEY_ROWS", 2)
    _revoked(db, tenant, 2, days_ago=401)
    _revoked(db, tenant, 1, days_ago=399)
    old = db.query(AgentApiKey).filter(AgentApiKey.name == "gone-401-0").one()
    db.add(AuditEvent(tenant_id=tenant.id, actor="rest", action="read", result="ok",
                      api_key_id=old.id))
    db.commit()
    key_service.create_api_key(db, tenant_id=tenant.id, name="fits")     # 1 row counted before
    with pytest.raises(key_service.TooManyKeys):
        key_service.create_api_key(db, tenant_id=tenant.id, name="three")
    assert key_service.purge_revoked(db) == 2
    names = {k.name for k in db.query(AgentApiKey)}
    assert names == {"gone-399-0", "fits"}
    assert db.query(AuditEvent).one().api_key_id is None


def test_no_revoked_key_is_purged_when_the_trail_is_kept_for_ever_and_every_one_counts(
        db, tenant, monkeypatch):
    from korely_agent import config

    _revoked(db, tenant, 2, days_ago=4000)
    monkeypatch.setattr(config.get_settings(), "audit_retention_days", 0)
    monkeypatch.setattr(key_service, "MAX_KEY_ROWS", 2)
    assert key_service.purge_revoked(db) == 0
    assert db.query(AgentApiKey).count() == 2
    with pytest.raises(key_service.TooManyKeys, match="for ever"):
        key_service.create_api_key(db, tenant_id=tenant.id, name="third")


def test_a_revoked_key_with_a_batch_job_not_finished_is_kept_and_still_stops_it(db, tenant):
    """The review of 25 Nov 2026: the purge set the job's key NULL, which the
    worker reads as a job queued before keys were recorded, and the revoked
    key's job imported. The key stays until its job has ended."""
    from datetime import datetime, timedelta, timezone

    from korely_agent.services import batch

    row, _full = key_service.create_api_key(db, tenant_id=tenant.id, name="queued-with")
    job = batch.enqueue_batch(db, tenant.id, [{"content": "x", "user_id": "u"}], api_key_id=row.id)
    row.revoked_at = datetime.now(timezone.utc) - timedelta(days=401)
    done, _full = key_service.create_api_key(db, tenant_id=tenant.id, name="nothing-queued")
    done.revoked_at = datetime.now(timezone.utc) - timedelta(days=401)
    db.commit()
    assert key_service.purge_revoked(db) == 1
    db.expire_all()
    assert {k.name for k in db.query(AgentApiKey)} == {"queued-with"}
    job = db.get(type(job), job.id)
    assert job.api_key_id == row.id and not batch._key_still_writes(db, job)
    job.status = batch.STATUS_COMPLETED
    db.commit()
    assert key_service.purge_revoked(db) == 1, "the key stayed after its job had ended"


def test_the_purge_deletes_in_blocks(db, tenant, monkeypatch):
    monkeypatch.setattr(key_service, "PURGE_BLOCK", 2)
    _revoked(db, tenant, 5, days_ago=401)
    assert key_service.purge_revoked(db) == 5
    assert db.query(AgentApiKey).count() == 0


def test_a_failed_job_with_a_retry_to_come_keeps_its_key_until_it_ends(db, tenant):
    """The review's probe p1, second review of 25 Nov 2026: a job left
    'processing' by a worker that died keeps its revoked key; reaped to
    'failed' with an attempt to come, it still does; the next pass ends it
    (the key no longer writes) and the key goes. None of its items is
    written."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import text

    from korely_agent.db.models import AgentBatchJob
    from korely_agent.services import batch

    row, _full = key_service.create_api_key(db, tenant_id=tenant.id, name="k")
    job = batch.enqueue_batch(db, tenant.id, [{"content": "x", "user_id": "u"}], api_key_id=row.id)
    row.revoked_at = datetime.now(timezone.utc) - timedelta(days=401)
    job.status = batch.STATUS_PROCESSING
    job.attempts = 1
    db.commit()
    db.execute(text("UPDATE agent_batch_jobs SET updated_at = now() - interval '2 days' WHERE id = :i"),
               {"i": job.id})
    db.commit()
    assert key_service.purge_revoked(db) == 0, "a job being worked lost its key"
    batch.reclaim_interrupted(db)
    db.expire_all()
    assert db.get(AgentBatchJob, job.id).status == batch.STATUS_FAILED
    assert key_service.purge_revoked(db) == 0, "a job with a retry to come lost its key"
    batch.process_batch_jobs(db, limit=5)
    db.expire_all()
    assert db.get(AgentBatchJob, job.id).imported == 0
    assert key_service.purge_revoked(db) == 1, "the key stayed after its job had ended"


def test_a_pending_job_with_every_attempt_spent_does_not_keep_its_key(db, tenant):
    """A job pending with no attempt left is never claimed again, so it does
    not keep its revoked key for ever (second review of 25 Nov 2026)."""
    from datetime import datetime, timedelta, timezone

    from korely_agent.services import batch

    row, _full = key_service.create_api_key(db, tenant_id=tenant.id, name="k")
    job = batch.enqueue_batch(db, tenant.id, [{"content": "x", "user_id": "u"}], api_key_id=row.id)
    job.attempts = job.max_attempts
    row.revoked_at = datetime.now(timezone.utc) - timedelta(days=401)
    db.commit()
    assert key_service.purge_revoked(db) == 1


def test_the_purge_finds_by_index_what_points_at_a_key(db):
    """The second review of 25 Nov 2026: each key deleted sets NULL the rows
    that point at it, and without these indexes each read the whole trail.
    In the models (this database) and in the migration that makes them."""
    import importlib.util
    from pathlib import Path

    from sqlalchemy import text

    have = {r for (r,) in db.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE tablename IN ('audit_events', 'agent_batch_jobs')"))}
    for table in ("audit_events", "agent_batch_jobs"):
        assert any(f"ON public.{table} USING btree (api_key_id)" in d for d in have), table
    path = next(Path(__file__).resolve().parent.parent.glob("alembic/versions/b7e3f1a9c2d4_*.py"))
    spec = importlib.util.spec_from_file_location("b7e3f1a9c2d4", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert {t for _n, t in migration._INDEXES} == {"audit_events", "agent_batch_jobs"}


def test_one_condition_says_a_job_is_unfinished_for_the_keys_and_the_cloud(db, tenant):
    """The third review of 25 Nov 2026: the Cloud's agent cap (through
    batch.UNFINISHED_JOB) counted a pending job with every attempt spent as
    not finished, the purge of keys did not. One condition now, in both
    forms: the same jobs read through the SQL text and the expression."""
    from sqlalchemy import text

    from korely_agent.db.models import AgentBatchJob
    from korely_agent.services import batch

    states = [("pending", 0), ("pending", 3), ("processing", 3), ("failed", 1), ("failed", 3),
              ("completed", 1)]
    ids = {}
    for status, attempts in states:
        job = batch.enqueue_batch(db, tenant.id, [{"content": "x", "user_id": "u"}])
        job.status, job.attempts, job.max_attempts = status, attempts, 3
        ids[job.id] = (status, attempts)
    db.commit()
    by_text = {i for (i,) in db.execute(text(
        "SELECT j.id FROM agent_batch_jobs j WHERE j.tenant_id = :t AND " + batch.UNFINISHED_JOB),
        {"t": tenant.id})}
    by_expression = {i for (i,) in db.query(AgentBatchJob.id).filter(
        AgentBatchJob.tenant_id == tenant.id, batch.unfinished())}
    assert by_text == by_expression
    assert sorted(ids[i] for i in by_text) == [("failed", 1), ("pending", 0), ("processing", 3)]
