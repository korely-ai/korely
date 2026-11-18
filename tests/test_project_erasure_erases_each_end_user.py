"""Erasing a project counts as erasing each of its end users (audit of
19 Oct 2026).

DELETE /admin/projects/{id}?erase=true filed one erasure record, scope
'project' and no end user, and `pseudonyms.as_logged` knows an end user is
erased only from a scope 'user' record under their pseudonym. So a trail row
written after the erasure, by work in flight when it committed or by a later
read through a key the erasure moved to the default project, kept the erased
id in clear, next to that person's pseudonymised rows, for
AUDIT_RETENTION_DAYS. Now the erasure also files one scope 'user' record per
end user it erased, under their pseudonym, made in the project
(`erased_in_project_id`) and naming it (`target_id`), with no counts: the
counts stay on the project's own record, whose id is still the `audit_id`."""
import threading
import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from korely_agent.db.models import (AgentBatchJob, AgentForgetAudit, AgentMemory, AgentProject,
                                    AuditEvent)
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service
from korely_agent.services import pseudonyms


EU = "anna@example.com"
OTHER = "bruno@example.com"


@pytest.fixture
def keyed(monkeypatch):
    from korely_agent.config import get_settings

    monkeypatch.setenv("SECRET_KEY", "a-long-random-string-for-the-tests")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


def _memory(db, tenant, pid, user, text="user: I live in Milan."):
    db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=user,
                       content=text, graph_sync_status="done"))
    db.commit()


def _pilot(db, tenant):
    default = project_service.get_or_create_default_project(db, tenant.id)
    pilot = project_service.create_project(db, tenant_id=tenant.id, name="Pilot")
    _memory(db, tenant, pilot.id, EU)
    _memory(db, tenant, pilot.id, OTHER)
    return default, pilot


def _per_user(db, tenant):
    db.expire_all()
    return sorted((r.end_user_id, r.erased_in_project_id, r.target_id, r.memories_forgotten,
                   r.facts_invalidated)
                  for r in db.query(AgentForgetAudit).filter(AgentForgetAudit.tenant_id == tenant.id,
                                                             AgentForgetAudit.scope == "user"))


# ── the records ─────────────────────────────────────────────────────────────

def test_each_end_user_erased_has_a_record_of_their_own(keyed, db, tenant):
    default, pilot = _pilot(db, tenant)
    out = project_service.delete_project(db, tenant.id, db.get(AgentProject, pilot.id), erase=True)

    per_user = _per_user(db, tenant)
    assert per_user == sorted((pseudonyms.pseudonym(tenant.id, u), pilot.id, str(pilot.id), 0, 0)
                              for u in (EU, OTHER))
    assert EU not in str(per_user) and OTHER not in str(per_user)
    (project_record,) = db.query(AgentForgetAudit).filter(AgentForgetAudit.scope == "project").all()
    from korely_agent.services.ids import audit_public_id
    assert out["audit_id"] == audit_public_id(project_record.id), "one audit_id, the project's"
    assert (project_record.memories_forgotten, project_record.end_user_id) == (2, None)
    # Filed where the project's own record is, so a sum of the counts reads 2 once.
    assert {r.project_id for r in db.query(AgentForgetAudit)} == {default.id}
    assert sum(r.memories_forgotten for r in db.query(AgentForgetAudit)) == 2
    assert pseudonyms.as_logged(db, tenant.id, EU) == pseudonyms.pseudonym(tenant.id, EU)


def test_an_end_user_only_in_a_queued_batch_is_erased_too(keyed, db, tenant):
    """Their item is all the project holds of them: the erasure deletes the
    job, and a later call naming them wrote their id in clear."""
    _default, pilot = _pilot(db, tenant)
    db.add(AgentBatchJob(tenant_id=tenant.id, project_id=pilot.id, received=1,
                         payload=[{"content": "Carla prefers email.", "user_id": "carla"}]))
    db.commit()
    project_service.delete_project(db, tenant.id, db.get(AgentProject, pilot.id), erase=True)
    assert pseudonyms.pseudonym(tenant.id, "carla") in {r[0] for r in _per_user(db, tenant)}
    assert pseudonyms.as_logged(db, tenant.id, "carla") == pseudonyms.pseudonym(tenant.id, "carla")


def test_without_a_key_only_the_projects_record(db, tenant):
    """No pseudonym to name them by: the proof is the project's audit_id, as
    for an end user erased without SECRET_KEY."""
    _default, pilot = _pilot(db, tenant)
    project_service.delete_project(db, tenant.id, db.get(AgentProject, pilot.id), erase=True)
    assert [r.scope for r in db.query(AgentForgetAudit)] == ["project"]


# ── what is written after it ────────────────────────────────────────────────

def _erase_while_searching(db, tenant, engine, client, monkeypatch):
    """A search of the project's key naming EU, whose query vector is being
    computed when the project's erasure commits in another session; then the
    README's check, read back through the same key, which the erasure moved
    to the default project."""
    from korely_agent.core.llm import EMBEDDING_DIM
    from korely_agent.services import memory_engine

    default, pilot = _pilot(db, tenant)
    _k, plain = key_service.create_api_key(db, tenant_id=tenant.id, name="pilot-agent",
                                           scopes=["memories:read"], project_id=pilot.id)
    head = {"Authorization": f"Bearer {plain}"}
    erased = {}

    def erase():
        s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
        try:
            erased["out"] = project_service.delete_project(
                s, tenant.id, project_service.get_project(s, tenant.id, pilot.id), erase=True)
        finally:
            s.close()

    def embed_while_erased(text, task=None, **kw):
        t = threading.Thread(target=erase)
        t.start()
        t.join(60)
        return [0.01] * EMBEDDING_DIM

    monkeypatch.setattr(memory_engine, "embed", embed_while_erased)
    r = client.post("/v1/memories/search", json={"query": "where", "user_id": EU}, headers=head)
    assert r.status_code == 200, r.text
    assert erased.get("out", {}).get("audit_id"), "the erasure committed during the search"
    monkeypatch.undo()
    r = client.get("/v1/memories", params={"user_id": EU}, headers=head)
    assert r.status_code == 200 and r.json()["total"] == 0, r.text
    return default, head


def test_a_trail_row_written_after_the_erasure_carries_the_pseudonym(
        keyed, db, tenant, engine, client, monkeypatch):
    default, _head = _erase_while_searching(db, tenant, engine, client, monkeypatch)
    db.expire_all()
    rows = [((e.meta or {}).get("endpoint"), e.end_user_id, e.project_id)
            for e in db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id,
                                                 AuditEvent.end_user_id.isnot(None))]
    p = pseudonyms.pseudonym(tenant.id, EU)
    assert ("memories:search", p, default.id) in rows, rows
    assert ("memories:list", p, default.id) in rows, rows
    assert EU not in {r[1] for r in rows}, "the erased id in clear in the trail"


def test_get_audit_by_the_id_finds_the_proof(keyed, db, tenant, engine, client, monkeypatch):
    _default, head = _erase_while_searching(db, tenant, engine, client, monkeypatch)
    r = client.get("/v1/audit", params={"user_id": EU}, headers=head)
    assert r.status_code == 200, r.text
    events = r.json()["events"]
    assert {e["meta"]["endpoint"] for e in events} >= {"memories:search", "memories:list"}
    assert all(e["user_id"] == pseudonyms.pseudonym(tenant.id, EU) for e in events), events


# ── the answer ──────────────────────────────────────────────────────────────

def test_the_project_erasures_answer_is_unchanged(keyed, db, tenant, client):
    """One erasure, one answer, one audit_id: the project's. The records of
    its end users are filed beside it and named in no answer."""
    from korely_agent.services.ids import audit_public_id

    _row, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="admin",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    admin = {"Authorization": f"Bearer {full}"}
    _default, pilot = _pilot(db, tenant)
    r = client.delete(f"/admin/projects/{pilot.id}", headers=admin, params={"erase": "true"})
    assert r.status_code == 200, r.text
    body = r.json()
    project_record = db.query(AgentForgetAudit).filter(AgentForgetAudit.scope == "project").one()
    assert body == {"deleted": True, "keys_moved": 0, "apps_disconnected": 0, "keys_revoked": 0,
                    "memories_moved": 0, "facts_moved": 0, "facts_merged": 0,
                    "aliases_moved": 0, "aliases_not_moved": 0,
                    "memories_deleted": 2, "facts_deleted": 0, "batches_deleted": 0,
                    "erasure": "permanent", "audit_id": audit_public_id(project_record.id),
                    # Where what moved is written (1.0.19): no writer here.
                    "region": None}
    assert len(_per_user(db, tenant)) == 2
    erase_events = db.query(AuditEvent).filter(AuditEvent.action == "erase").all()
    assert [(e.target_id, e.end_user_id) for e in erase_events] == [(str(pilot.id), None)], \
        "one erase in the trail: the Requests page counts one write"
