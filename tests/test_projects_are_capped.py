"""An account's projects are capped, as its keys and its declared names
(the leftover of the whole-read guard of the audit of 27 Oct 2026): the project
list, the Projects page and the Cloud's claim read every project of an account,
and an account could make as many as it liked, so those reads grew with a
number a key holder chooses."""
import threading

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentProject
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service


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


def test_a_ceiling_of_projects_and_room_again_after_a_delete(db, tenant, client, admin, monkeypatch):
    monkeypatch.setattr(project_service, "MAX_PROJECTS", 3)
    project_service.get_or_create_default_project(db, tenant.id)   # one
    for name in ("two", "three"):
        assert client.post("/admin/projects", headers=admin, json={"name": name}).status_code == 201
    r = client.post("/admin/projects", headers=admin, json={"name": "four"})
    assert r.status_code == 409 and r.json()["code"] == "too_many_projects", r.text
    assert "3" in r.json()["message"]
    # An archived project counts: /unarchive brings it back with no check.
    two = db.query(AgentProject).filter(AgentProject.tenant_id == tenant.id,
                                        AgentProject.name == "two").one()
    assert client.post(f"/admin/projects/{two.id}/archive", headers=admin).status_code == 200
    r = client.post("/admin/projects", headers=admin, json={"name": "four"})
    assert r.status_code == 409 and r.json()["code"] == "too_many_projects"
    # A deleted one does not.
    assert client.delete(f"/admin/projects/{two.id}", headers=admin).status_code == 200
    assert client.post("/admin/projects", headers=admin, json={"name": "four"}).status_code == 201


def test_the_default_project_is_never_refused(db, tenant, monkeypatch):
    """The Default is the fallback of every key: an account with none gets it
    whatever it holds (GET /admin/projects makes it)."""
    monkeypatch.setattr(project_service, "MAX_PROJECTS", 0)
    assert project_service.get_or_create_default_project(db, tenant.id).is_default


def test_two_creates_at_once_never_pass_the_ceiling(db, tenant, monkeypatch):
    """The count and the commit under one lock of the account (as for keys):
    two creates that counted together would both have gone in."""
    from korely_agent.db import get_own_sessionmaker

    monkeypatch.setattr(project_service, "MAX_PROJECTS", 2)
    project_service.get_or_create_default_project(db, tenant.id)
    made = get_own_sessionmaker()
    out, start = [], threading.Barrier(4)

    def create(i):
        s = made()
        try:
            start.wait()
            project_service.create_project(s, tenant_id=tenant.id, name=f"racer-{i}")
            out.append("made")
        except project_service.TooManyProjects:
            out.append("refused")
        finally:
            s.close()

    runners = [threading.Thread(target=create, args=(i,)) for i in range(4)]
    for t in runners:
        t.start()
    for t in runners:
        t.join(30)
    assert sorted(out) == ["made", "refused", "refused", "refused"], out
    held = db.query(AgentProject).filter(AgentProject.tenant_id == tenant.id).count()
    assert held == 2


def test_the_ceiling_is_a_project_refusal_with_its_code():
    assert issubclass(project_service.TooManyProjects, project_service.ProjectRefused)
    assert project_service.TooManyProjects.code == "too_many_projects"
    assert project_service.MAX_PROJECTS == 100


def test_the_new_project_dialog_says_the_ceiling():
    """The 409's code read by the dialog, not "Something went wrong"."""
    from pathlib import Path

    dialog = (Path(__file__).resolve().parent.parent / "dashboard" / "src" / "components"
              / "CreateProjectDialog.tsx").read_text(encoding="utf-8")
    assert 'case "too_many_projects":' in dialog
