"""The one door a link leaves by (memory_core 6.20.0, `objects.remove_link`;
services/facts/subjects.remove): the closings the judge made through a link
between two subjects are given back first, and only those, whoever removes
it: the dashboard, a memory's forget or erasure, an agent's deletion. A
statement that deletes the link past the door fails on the database's
constraint; an account's erasure, which takes the facts with it, passes."""
import pytest
from sqlalchemy.exc import IntegrityError

from korely_agent.db.models import AgentFact, AgentFactClosureLink, AgentMemory, AgentObjectLink, Tenant
from korely_agent.services.facts import contradiction, erasure, subjects
from korely_agent.services.memory_core import objects as O
from korely_agent.services.facts.entities import TABLES
from tests.subject_world import Judge, fact, memory, vec, wall_link


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(subjects, "SAME_SUBJECT_REVIEW", True)
    j = Judge()
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: j)
    return j


def closed_through_a_link(db, tenant):
    """"Korely Cloud has_version 1.0.30" (agent ops-agent) closed by "Cloud di
    Korely runs_version 1.0.35" (agent planner) through the link, whose
    words came from a third memory of agent docs-agent, which states neither."""
    a = memory(db, tenant, "Korely Cloud has version 1.0.30.", agent="ops-agent", days_ago=5)
    b = memory(db, tenant, "Cloud di Korely runs 1.0.35.", agent="planner", days_ago=1)
    w = memory(db, tenant, "Korely Cloud is served by Caddy.", agent="docs-agent", days_ago=3)
    old = fact(db, tenant, "Korely Cloud", "has_version", "1.0.30", mem=a, agent="ops-agent", minutes_ago=3000,
               valid_days_ago=5, vector=vec(5))
    fact(db, tenant, "Korely Cloud", "served_by", "Caddy", mem=w, agent="docs-agent", minutes_ago=2000,
         vector=vec(40))
    new = fact(db, tenant, "Cloud di Korely", "runs_version", "1.0.35", mem=b, agent="planner",
               minutes_ago=1, valid_days_ago=1, vector=vec(5))
    link = wall_link(db, tenant, "Korely Cloud", "Cloud di Korely", source=w, canonical_memory=b)
    assert contradiction.detect_and_invalidate(
        db, tenant.id, end_user_id=None, new_fact_id=new.id, new_subject=new.subject_label,
        new_predicate_raw=new.predicate_raw, new_predicate_canonical=new.predicate_canonical,
        new_object=new.object_label, new_embedding=new.embedding, new_valid_from=new.valid_from,
        new_subject_canonical=new.subject_canonical, new_object_canonical=new.object_canonical,
        source=new.source_memory_id) == [old.id]
    db.commit()
    assert db.query(AgentFactClosureLink).count() == 1
    return old, new, link, w


def open_rows(db, tenant, obj):
    db.expire_all()
    return db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id, AgentFact.object_label == obj,
                                      AgentFact.invalid_at.is_(None)).all()


def test_the_door_gives_back_the_closing_as_a_new_row(db, tenant, on):
    old, new, link, _w = closed_through_a_link(db, tenant)
    closed_at = db.get(AgentFact, old.id).invalid_at
    assert subjects.remove(db, db.get(AgentObjectLink, link.id)) == 1
    db.commit()
    [back] = open_rows(db, tenant, "1.0.30")
    assert back.id != old.id and back.valid_from == closed_at, \
        "a new row from where it was closed: the closing never was"
    kept = db.get(AgentFact, old.id)
    assert kept.invalid_at == closed_at and kept.invalidated_by_id == new.id, "the store's history stays"
    assert db.query(AgentObjectLink).count() == 0 and db.query(AgentFactClosureLink).count() == 0
    assert db.get(AgentFact, new.id).invalid_at is None


def test_only_the_closings_of_that_link_come_back(db, tenant, on):
    old, new, link, _w = closed_through_a_link(db, tenant)
    m = memory(db, tenant, "Cloud di Korely runs 1.0.34.", agent="planner", days_ago=2)
    own = fact(db, tenant, "Cloud di Korely", "runs_version", "1.0.34", mem=m, agent="planner",
               minutes_ago=1500, valid_days_ago=2, vector=vec(5))
    own.invalid_at, own.invalidated_by_id = new.valid_from, new.id
    db.commit()
    subjects.remove(db, db.get(AgentObjectLink, link.id))
    db.commit()
    assert len(open_rows(db, tenant, "1.0.30")) == 1
    assert open_rows(db, tenant, "1.0.34") == [], "closed by its own name's fact: not the link's"


def test_the_admin_route_takes_the_door(db, tenant, on):
    from fastapi.testclient import TestClient

    from korely_agent.db import get_db
    from korely_agent.main import create_app
    from korely_agent.services import keys as key_service

    old, _new, link, _w = closed_through_a_link(db, tenant)
    _row, raw = key_service.create_api_key(
        db, tenant_id=tenant.id, name="admin",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app)
    h = {"Authorization": f"Bearer {raw}"}
    [listed] = c.get("/admin/object-links", headers=h).json()["object_links"]
    assert listed["scope"] == "wall"
    assert c.delete(f"/admin/object-links/{link.id}", headers=h).json()["deleted"] is True
    assert len(open_rows(db, tenant, "1.0.30")) == 1


@pytest.mark.parametrize("how", ["forget", "erase"])
def test_a_memorys_forget_or_erasure_keeps_a_link_whose_names_still_have_facts(db, tenant, on, how):
    """The link is a statement about two names, not about the memory whose
    fact was first (the review of 23 Nov 2026, P5): forgetting that memory
    leaves the link as it is; erasing it points the link at another live
    memory of the name. The old version stays closed."""
    old, _new, link, w = closed_through_a_link(db, tenant)
    if how == "forget":
        erasure.forget_memory(db, tenant.id, w.id, project_id=None)
    else:
        erasure.erase_memory(db, tenant.id, w.id, project_id=None)
    db.commit()
    db.expire_all()
    [kept] = db.query(AgentObjectLink).all()
    assert kept.id == link.id
    assert kept.source_memory_id == (w.id if how == "forget" else old.source_memory_id), \
        "erased, it points at the live memory of the name's fact"
    assert open_rows(db, tenant, "1.0.30") == [], "the old version stays closed"
    assert db.query(AgentFactClosureLink).count() == 1


def test_erasing_every_fact_of_a_name_takes_the_link_by_the_door(db, tenant, on):
    """When one of the two names has no fact left in the wall, open or
    closed, the link leaves by the door: nothing is left for it to explain."""
    old, _new, _link, w = closed_through_a_link(db, tenant)
    erasure.erase_memory(db, tenant.id, w.id, project_id=None)
    erasure.erase_memory(db, tenant.id, old.source_memory_id, project_id=None)
    db.commit()
    assert db.query(AgentObjectLink).count() == 0
    assert db.query(AgentFactClosureLink).count() == 0


def test_an_agents_deletion_points_the_link_at_another_memory(db, tenant, on):
    """The link's first fact came from agent `docs-agent`; deleting it
    points the link at the live memory of another agent's fact of the name,
    and the version that agent ops-agent's fact held stays closed."""
    old, _new, link, _w = closed_through_a_link(db, tenant)
    erasure.delete_agent(db, tenant.id, "docs-agent", project_id=None)
    db.commit()
    db.expire_all()
    [kept] = db.query(AgentObjectLink).all()
    assert kept.id == link.id and kept.source_memory_id == old.source_memory_id
    assert open_rows(db, tenant, "1.0.30") == []


def test_an_agents_deletion_that_takes_a_names_last_fact_takes_the_link(db, tenant, on):
    """Every fact of "Cloud di Korely" is agent planner's: deleting it leaves
    the name with no fact, and the link leaves by the door, giving back the
    version it held closed (agent ops-agent's, which stays)."""
    _old, _new, _link, _w = closed_through_a_link(db, tenant)
    erasure.delete_agent(db, tenant.id, "planner", project_id=None)
    db.commit()
    assert db.query(AgentObjectLink).count() == 0
    [back] = open_rows(db, tenant, "1.0.30")
    assert back.agent_id == "ops-agent"


def test_a_bulk_delete_past_the_door_fails(db, tenant, on):
    _old, _new, link, w = closed_through_a_link(db, tenant)
    with pytest.raises(IntegrityError):
        db.query(AgentObjectLink).filter(AgentObjectLink.id == link.id).delete(synchronize_session=False)
        db.flush()
    db.rollback()
    with pytest.raises(IntegrityError):
        # the memory's cascade would take the link past the door
        db.query(AgentMemory).filter(AgentMemory.id == w.id).delete(synchronize_session=False)
        db.flush()
    db.rollback()
    with pytest.raises(O.LinkHoldsClosures):
        O.remove_link(db, TABLES, db.get(AgentObjectLink, link.id))
    db.rollback()
    assert db.query(AgentObjectLink).count() == 1 and db.query(AgentFactClosureLink).count() == 1


def test_an_accounts_erasure_passes(db, tenant, on):
    closed_through_a_link(db, tenant)
    db.query(Tenant).filter(Tenant.id == tenant.id).delete(synchronize_session=False)
    db.commit()
    assert db.query(AgentObjectLink).count() == 0 and db.query(AgentFactClosureLink).count() == 0


def _project(db, tenant, name, default=False):
    import uuid

    from korely_agent.db.models import AgentProject

    p = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name=name, slug=name.lower(), is_default=default)
    db.add(p)
    db.commit()
    return p


@pytest.mark.parametrize("default_holds_the_name", [False, True], ids=["moves", "stays-out"])
def test_a_projects_deletion_moves_the_link_or_takes_the_door(db, tenant, on, default_holds_the_name):
    """A deleted project's links move into the default project with its facts
    (services/projects._move_links): a wall's link joins the default
    project's wall, closings kept; one whose name the default wall has
    decided already leaves by the door, and its closing comes back."""
    from korely_agent.services import projects

    default = _project(db, tenant, "Default", default=True)
    beta = _project(db, tenant, "Beta")
    a = memory(db, tenant, "Korely Cloud has version 1.0.30.", agent="ops-agent", days_ago=5, project_id=beta.id)
    b = memory(db, tenant, "Cloud di Korely runs 1.0.35.", agent="planner", days_ago=1, project_id=beta.id)
    old = fact(db, tenant, "Korely Cloud", "has_version", "1.0.30", mem=a, agent="ops-agent", minutes_ago=3000,
               valid_days_ago=5, vector=vec(5), project_id=beta.id)
    new = fact(db, tenant, "Cloud di Korely", "runs_version", "1.0.35", mem=b, agent="planner", minutes_ago=1,
               valid_days_ago=1, vector=vec(5), project_id=beta.id)
    wall_link(db, tenant, "Korely Cloud", "Cloud di Korely", source=a, project_id=beta.id)
    if default_holds_the_name:
        wall_link(db, tenant, "Korely Cloud", "Korely", project_id=default.id)
    assert contradiction.detect_and_invalidate(
        db, tenant.id, project_id=beta.id, end_user_id=None, new_fact_id=new.id, new_subject=new.subject_label,
        new_predicate_raw=new.predicate_raw, new_predicate_canonical=new.predicate_canonical,
        new_object=new.object_label, new_embedding=new.embedding, new_valid_from=new.valid_from,
        new_subject_canonical=new.subject_canonical, new_object_canonical=new.object_canonical,
        source=new.source_memory_id) == [old.id]
    db.commit()
    projects.delete_project(db, tenant.id, db.get(type(beta), beta.id))
    db.commit()
    db.expire_all()
    if default_holds_the_name:
        assert db.query(AgentFactClosureLink).count() == 0
        assert len(open_rows(db, tenant, "1.0.30")) == 1, "it left by the door"
    else:
        [moved] = db.query(AgentObjectLink).filter(AgentObjectLink.canonical_label == "Cloud di Korely").all()
        assert moved.project_id == default.id and moved.scope == "wall"
        assert db.query(AgentFactClosureLink).count() == 1, "the closing moves with its link"
        assert open_rows(db, tenant, "1.0.30") == []
