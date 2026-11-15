"""A project's deletion never changes the region its keys and data are
written in (the security review of 12 Nov 2026).

Deleting a project moves what it held into the account's Default project
(services/projects.delete_project), and from then on the Default project's
writer writes it: the region is read from the project at every write
(services/regions.py). On the Cloud the Default project is Global, so an EU
project deleted without `erase` sent its keys, and its memories still in the
queue, to the Global writer, and neither the dialog nor the reply said so:
a fallback from Europe to Global, which the rule of the regions forbids.

Now the deletion is refused (409 `project_region_differs`) when live keys,
or (without `erase`) any memory, fact or batch job, would move into a
Default project of another region; nothing is deleted, and the reply of a
deletion that goes says where what moved is written (`region`).
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentApiKey, AgentFact, AgentMemory, AgentProject, OAuthClient
from korely_agent.services import keys as key_service
from korely_agent.services import projects, regions
from tests.test_regions import GEM, SCW, admin, pending, project, writers


@pytest.fixture
def client(db, monkeypatch):
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _writers(monkeypatch):
    from korely_agent.core import extensions, llm

    llm.reset_provider_cache()
    yield
    llm.reset_provider_cache()
    extensions.reset()


def default_in(db, tenant, region):
    d = projects.get_or_create_default_project(db, tenant.id)
    d.region = region
    db.commit()
    return d


def agent_key(db, tenant, p):
    row, _full = key_service.create_api_key(db, tenant_id=tenant.id, name=f"a-{uuid.uuid4().hex[:6]}",
                                            project_id=p.id, expires_in_days=None)
    return row


def where(db, model, row_id):
    db.expire_all()
    return db.get(model, row_id).project_id


# ── refused: something would move into another region ──────────────────────

def test_an_eu_project_is_not_moved_into_a_global_default(db, tenant, client, monkeypatch):
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    default_in(db, tenant, "global")
    europe = project(db, tenant, region="eu", name="Clinics")
    key = agent_key(db, tenant, europe)
    pending(db, tenant, europe, 1)
    queued = db.query(AgentMemory).filter(AgentMemory.project_id == europe.id).one().id

    r = client.delete(f"/admin/projects/{europe.id}", headers=admin(db, tenant))

    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "project_region_differs"
    assert "Europe" in body["message"] and "Global" in body["message"]
    assert "1 live API key" in body["message"] and "1 memory" in body["message"]
    db.expire_all()
    assert db.get(AgentProject, europe.id) is not None, "nothing deleted"
    assert where(db, AgentApiKey, key.id) == europe.id, "the key did not move"
    m = db.get(AgentMemory, queued)
    assert m.project_id == europe.id and m.graph_sync_status == "pending"
    assert regions.of_project(db, tenant.id, m.project_id) == "eu"


def test_the_erasure_with_a_live_key_is_refused_and_erases_nothing(db, tenant, client,
                                                                     monkeypatch):
    """Erased, the data goes, but the keys move: the agent's next writes
    would be the Global writer's. The erasure done before the count is
    rolled back with the rest."""
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    default_in(db, tenant, "global")
    europe = project(db, tenant, region="eu")
    key = agent_key(db, tenant, europe)
    pending(db, tenant, europe, 2)

    r = client.delete(f"/admin/projects/{europe.id}?erase=true", headers=admin(db, tenant))

    assert r.status_code == 409, r.text
    assert r.json()["code"] == "project_region_differs"
    assert r.json()["message"].startswith("Not erased:")
    assert "Revoke its keys first" in r.json()["message"]
    db.expire_all()
    assert db.query(AgentMemory).filter(AgentMemory.project_id == europe.id).count() == 2
    assert db.get(AgentApiKey, key.id).revoked_at is None


def test_the_erasure_goes_once_the_keys_are_revoked(db, tenant, client, monkeypatch):
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    default_in(db, tenant, "global")
    europe = project(db, tenant, region="eu")
    key = agent_key(db, tenant, europe)
    pending(db, tenant, europe, 2)
    assert key_service.revoke_api_key(db, key_id=key.id, tenant_id=tenant.id)

    r = client.delete(f"/admin/projects/{europe.id}?erase=true", headers=admin(db, tenant))

    assert r.status_code == 200, r.text
    assert r.json()["memories_deleted"] == 2 and r.json()["erasure"] == "permanent"
    db.expire_all()
    assert db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant.id).count() == 0


def test_a_key_that_no_longer_writes_does_not_hold_the_erasure(db, tenant, client, monkeypatch):
    """Live is what authentication accepts, as the archive reads it
    (projects._live_keys): an expired key writes nowhere, wherever it goes."""
    from datetime import datetime, timedelta, timezone

    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    default_in(db, tenant, "global")
    europe = project(db, tenant, region="eu")
    key_service.create_api_key(db, tenant_id=tenant.id, name="old", project_id=europe.id,
                               expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    pending(db, tenant, europe, 1)

    r = client.delete(f"/admin/projects/{europe.id}?erase=true", headers=admin(db, tenant))

    assert r.status_code == 200, r.text
    assert r.json()["memories_deleted"] == 1


@pytest.mark.parametrize("held", ["fact", "batch job"])
def test_a_fact_or_a_queued_import_holds_the_deletion_too(db, tenant, client, monkeypatch, held):
    """A fact moved into Default is read by its judge, a queued import is
    written there: each is enough to refuse."""
    from korely_agent.services import batch

    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    default_in(db, tenant, "global")
    europe = project(db, tenant, region="eu")
    if held == "fact":
        # Written straight to /v1/facts: no source memory.
        db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=europe.id,
                         end_user_id="ada", subject_label="Ada", subject_canonical="ada",
                         subject_type="person", predicate_raw="prefers",
                         predicate_canonical="prefers", object_label="tea",
                         object_canonical="tea", confidence=0.9, tense="current",
                         model_used="a-writer"))
        db.commit()
    else:
        batch.enqueue_batch(db, tenant.id, [{"content": "x", "user_id": "ada"}],
                            project_id=europe.id)

    r = client.delete(f"/admin/projects/{europe.id}", headers=admin(db, tenant))

    assert r.status_code == 409 and f"1 {held}" in r.json()["message"], r.text


def test_a_region_chosen_is_not_handed_to_the_installs_default(db, tenant, client, monkeypatch):
    """On an install of one's own a Default project with no region follows
    the install's default writer, the provider saved last: Europe today,
    Global the day a Global provider is saved. A project that chose Europe
    does not move into it."""
    writers(monkeypatch, "eu", eu=SCW, **{"global": GEM})
    default_in(db, tenant, None)
    europe = project(db, tenant, region="eu")
    agent_key(db, tenant, europe)
    assert regions.of_project(db, tenant.id, europe.id) == "eu"

    r = client.delete(f"/admin/projects/{europe.id}", headers=admin(db, tenant))

    assert r.status_code == 409, r.text
    assert "following this install's default writer" in r.json()["message"]


def test_a_key_the_erasure_revokes_does_not_hold_it(db, tenant, client, monkeypatch):
    """The check counts the keys left live after the erasure: a key
    confined to the erased end users is revoked by it, and does not stop it
    (the review of 12 Nov 2026: the order of the two was held by no test)."""
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    default_in(db, tenant, "global")
    europe = project(db, tenant, region="eu")
    confined, _full = key_service.create_api_key(db, tenant_id=tenant.id, name="conf",
                                                 project_id=europe.id, expires_in_days=None,
                                                 end_users=["u0"])
    pending(db, tenant, europe, 1)  # end user u0

    r = client.delete(f"/admin/projects/{europe.id}?erase=true", headers=admin(db, tenant))

    assert r.status_code == 200, r.text
    assert (r.json()["keys_revoked"], r.json()["memories_deleted"]) == (1, 1)
    db.expire_all()
    assert db.get(AgentApiKey, confined.id).revoked_at is not None


def test_a_refused_deletion_leaves_no_default_behind(db, tenant, monkeypatch):
    """No Default yet: the deletion makes it in its own transaction, and the
    refusal takes it back (get_or_create_default_project, commit=False)."""
    writers(monkeypatch, "eu", eu=SCW, **{"global": GEM})
    europe = project(db, tenant, region="eu")
    agent_key(db, tenant, europe)

    with pytest.raises(projects.ProjectRegionDiffers):
        projects.delete_project(db, tenant.id, europe)

    db.expire_all()
    assert db.query(AgentProject).filter(AgentProject.tenant_id == tenant.id,
                                         AgentProject.is_default.is_(True)).count() == 0


# ── a change of the Default project's region at the same time ──────────────
# The review of 12 Nov 2026: the Default project's region was read without a
# lock, so a PATCH committed between the check and the deletion's commit
# moved an EU project's key into a Global Default, and the answer said eu.

def _waiting_on_a_lock(engine, statement) -> bool:
    from sqlalchemy import text

    with engine.connect() as c:
        return bool(c.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock' AND query ILIKE :q"), {"q": statement + "%"}).scalar())


def _until(condition, seconds=10.0) -> bool:
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_a_region_change_of_the_default_waits_for_the_deletion(db, tenant, monkeypatch):
    """The deletion checked first: the PATCH of Default's region waits for
    its commit, so the answer's `region` is the one the move was made in."""
    import threading

    from sqlalchemy.orm import sessionmaker

    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    default = default_in(db, tenant, "eu")
    europe = project(db, tenant, region="eu")
    key = agent_key(db, tenant, europe)
    pending(db, tenant, europe, 1)
    checked, go_on = threading.Event(), threading.Event()
    folded = projects._fold_duplicates

    def after_the_check(*a, **k):
        checked.set()
        go_on.wait(10)
        return folded(*a, **k)

    monkeypatch.setattr(projects, "_fold_duplicates", after_the_check)
    engine = db.get_bind()
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    out = {}

    def delete():
        with Session() as s:
            out["reply"] = projects.delete_project(s, tenant.id, s.get(AgentProject, europe.id))

    def patch():
        with Session() as s:
            projects.rename_project(s, s.get(AgentProject, default.id), region="global")
            # Committed after the deletion's commit: the project is gone.
            out["patched_after_the_deletion"] = (
                s.query(AgentProject).filter(AgentProject.id == europe.id).count() == 0)

    deleting = threading.Thread(target=delete)
    deleting.start()
    assert checked.wait(10)
    patching = threading.Thread(target=patch)
    patching.start()
    waited = _until(lambda: _waiting_on_a_lock(engine, "UPDATE agent_projects"))
    go_on.set()
    deleting.join(30)
    patching.join(30)

    assert waited, "the PATCH did not wait for the deletion"
    assert out["reply"]["region"] == "eu" and out["patched_after_the_deletion"] is True
    db.expire_all()
    assert db.get(AgentApiKey, key.id).project_id == default.id


def test_a_region_change_committed_first_is_the_one_read(db, tenant, monkeypatch):
    """The PATCH first, not yet committed: the deletion waits for it, reads
    Global, and refuses; nothing moves."""
    import threading

    from sqlalchemy.orm import sessionmaker

    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    default = default_in(db, tenant, "eu")
    europe = project(db, tenant, region="eu")
    key = agent_key(db, tenant, europe)
    pending(db, tenant, europe, 1)
    engine = db.get_bind()
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    out = {}

    patching = Session()
    patching.query(AgentProject).filter(AgentProject.id == default.id).update(
        {AgentProject.region: "global"}, synchronize_session=False)

    def delete():
        with Session() as s:
            try:
                out["reply"] = projects.delete_project(s, tenant.id, s.get(AgentProject, europe.id))
            except projects.ProjectRegionDiffers as e:
                out["refused"] = str(e)

    deleting = threading.Thread(target=delete)
    deleting.start()
    waited = _until(lambda: _waiting_on_a_lock(engine, "SELECT agent_projects"))
    patching.commit()
    patching.close()
    deleting.join(30)

    assert waited, "the deletion did not wait for the PATCH"
    assert "reply" not in out and "Global" in out["refused"]
    db.expire_all()
    assert db.get(AgentApiKey, key.id).project_id == europe.id


# ── what goes as before ─────────────────────────────────────────────────────

def test_the_same_region_moves_and_says_where(db, tenant, client, monkeypatch):
    """The way on: the Default project given Europe on purpose first."""
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    default = default_in(db, tenant, "global")
    europe = project(db, tenant, region="eu")
    key = agent_key(db, tenant, europe)
    pending(db, tenant, europe, 1)
    h = admin(db, tenant)
    assert client.patch(f"/admin/projects/{default.id}", json={"region": "eu"},
                        headers=h).status_code == 200

    r = client.delete(f"/admin/projects/{europe.id}", headers=h)

    assert r.status_code == 200, r.text
    assert (r.json()["keys_moved"], r.json()["memories_moved"], r.json()["region"]) == (1, 1, "eu")
    assert where(db, AgentApiKey, key.id) == default.id


@pytest.mark.parametrize("own, of_default", [(None, None), ("global", "global"), (None, "global")])
def test_projects_of_one_region_move_as_before(db, tenant, client, monkeypatch, own, of_default):
    """Both following the install, both Global, or one that followed a
    Global install into a Default chosen Global: nothing changes region."""
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    default_in(db, tenant, of_default)
    p = project(db, tenant, region=own)
    agent_key(db, tenant, p)
    pending(db, tenant, p, 1)

    r = client.delete(f"/admin/projects/{p.id}", headers=admin(db, tenant))

    assert r.status_code == 200, r.text
    assert (r.json()["keys_moved"], r.json()["memories_moved"], r.json()["region"]) == (1, 1, "global")


def test_a_project_that_moves_nothing_a_writer_reads_goes(db, tenant, client, monkeypatch):
    """No live key and nothing held: nothing changes region, and an EU
    project made by mistake can be deleted. An app's key is revoked, never
    moved, so it does not hold the deletion either."""
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    default_in(db, tenant, "global")
    europe = project(db, tenant, region="eu")
    db.add(OAuthClient(client_id="app-1", info={"client_id": "app-1", "redirect_uris": []}))
    db.commit()
    app_key, _full = key_service.create_api_key(db, tenant_id=tenant.id, name="app",
                                                project_id=europe.id, oauth_client_id="app-1",
                                                expires_in_days=None)

    r = client.delete(f"/admin/projects/{europe.id}", headers=admin(db, tenant))

    assert r.status_code == 200, r.text
    assert (r.json()["keys_moved"], r.json()["apps_disconnected"]) == (0, 1)
    db.expire_all()
    assert db.get(AgentApiKey, app_key.id).revoked_at is not None


# ── the Cloud ───────────────────────────────────────────────────────────────

def _on_the_cloud(monkeypatch):
    from korely_agent import config
    from korely_agent.core import llm

    cloud = config.get_settings().model_copy(update={"korely_mode": "hosted"})
    monkeypatch.setattr(config, "get_settings", lambda: cloud)
    monkeypatch.setattr(llm, "get_settings", lambda: cloud)


@pytest.mark.cloud
def test_on_the_cloud_an_eu_project_stays_out_of_the_global_default(db, tenant, monkeypatch):
    writers(monkeypatch, "eu", eu=SCW, **{"global": GEM})
    _on_the_cloud(monkeypatch)
    default_in(db, tenant, "global")
    europe = project(db, tenant, region="eu")
    agent_key(db, tenant, europe)

    with pytest.raises(projects.ProjectRegionDiffers) as refused:
        projects.delete_project(db, tenant.id, europe)
    assert "Europe" in str(refused.value) and "following" not in str(refused.value)


@pytest.mark.cloud
def test_on_the_cloud_a_default_with_no_region_is_global_whatever_was_saved(db, tenant,
                                                                           monkeypatch):
    """A Cloud Default made before 0.1.42 has no region and is Global, the
    service's, whichever provider the superadmin saved last: a Global
    project moves into it."""
    writers(monkeypatch, "eu", eu=SCW, **{"global": GEM})
    _on_the_cloud(monkeypatch)
    default_in(db, tenant, None)
    world = project(db, tenant, region="global")
    agent_key(db, tenant, world)

    out = projects.delete_project(db, tenant.id, world)

    assert out["keys"] == 1 and out["region"] == "global"


# ── the restore's replay repeats a deletion already made ────────────────────

def test_the_replay_erases_again_across_regions(db, tenant, monkeypatch):
    """An erasure is repeated after a restore whatever the regions are now:
    refusing it would bring the person back in clear."""
    from korely_agent import config
    from korely_agent.services import erasure_replay
    from tests.test_erasure_replay import _alive_in, _project_erased, _restore_projects

    monkeypatch.setenv("SECRET_KEY", "x" * 48)
    config.get_settings.cache_clear()
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    default, x, x_row, _proof = _project_erased(db, tenant)
    kept = erasure_replay.export(db)
    _restore_projects(db, tenant, [x_row], [(x.id, "maria", None, "X maria")])
    db.get(AgentProject, x.id).region = "eu"
    db.get(AgentProject, default.id).region = "global"
    db.commit()
    agent_key(db, tenant, db.get(AgentProject, x.id))

    out = erasure_replay.replay(db, kept)

    assert out["replayed"] == {"project": 1}, out
    assert _alive_in(db, tenant) == []
    config.get_settings.cache_clear()
