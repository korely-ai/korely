"""DELETE /v1/memories/{id}?erase=true erases as every other erasure does
(audit of 18 Oct 2026): its /v1/batch item, the aliases and the vocabulary
example its facts alone taught go with it; a memory forgotten earlier can be
erased too; a restore makes the erasure again instead of a forget; and the
erasure of an end user in one project neither rewrites another project's
records nor leaves the id in clear in the account's own trail rows."""
import uuid
from datetime import datetime, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import (AgentBatchJob, AgentEntityAlias, AgentFact, AgentForgetAudit,
                                    AgentMemory, AgentProject, AuditEvent, PredicateCluster)
from korely_agent.services import batch, erasure_replay, pseudonyms
from korely_agent.services import facts as agent_facts
from korely_agent.services import keys as key_service
from korely_agent.services.ids import mem_public_id
from korely_agent.services.sentinels import EVERY_PROJECT
from tests.conftest import seeded_project_id



@pytest.fixture
def keyed(monkeypatch):
    from korely_agent.config import get_settings

    monkeypatch.setenv("SECRET_KEY", "a-long-random-string-for-the-tests")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def vectors(monkeypatch):
    from korely_agent.core import llm

    asked = []
    monkeypatch.setattr(llm, "embed", lambda text, **kw: asked.append(text) or [0.1] * EMBEDDING_DIM)
    monkeypatch.setattr(llm, "current_google_key", lambda: "k")
    return asked


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


def _head(db, tenant, project, **kw):
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}",
                                          project_id=project, expires_in_days=None, **kw)
    return {"Authorization": f"Bearer {full}"}


def _memory(db, tenant, project, user, obj, *, mid=None):
    m = AgentMemory(id=mid or uuid.uuid4(), tenant_id=tenant.id, project_id=project,
                    end_user_id=user, content=f"{user} has {obj}.", observation=f"{user}: {obj}",
                    embedding=[0.1] * EMBEDDING_DIM, graph_sync_status="done")
    db.add(m)
    db.commit()
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project, end_user_id=user,
                  subject_label=user, predicate_raw="has condition",
                  predicate_canonical="has_condition", object_label=obj, source_memory_id=m.id,
                  source_memory_ids=[str(m.id)], valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc))
    db.add(f)
    db.commit()
    return m.id, f.id


def test_the_copies_go_with_the_memory(keyed, db, tenant, client, vectors):
    pid = seeded_project_id(db, tenant.id)
    job = AgentBatchJob(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, status="completed",
                        received=2, imported=2,
                        payload=[{"content": "anna has type 2 diabetes.", "user_id": "anna"},
                                 {"content": "anna likes tea.", "user_id": "anna"}],
                        last_error="boom: anna has type 2 diabetes")
    db.add(job)
    db.commit()
    m, f = _memory(db, tenant, pid, "anna", "type 2 diabetes", mid=batch.item_memory_id(job.id, 0))
    _memory(db, tenant, pid, "bruno", "asthma")
    rel = PredicateCluster(tenant_id=tenant.id, project_id=pid, end_user_id="anna", canonical="has_condition",
                           example_object="type 2 diabetes", member_count=1)
    db.add(rel)
    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=pid, end_user_id="anna",
                            alias_lower="my doctor", alias_label="my doctor",
                            canonical_label="Dr Rossi", source_fact_id=f))
    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=pid, end_user_id="anna",
                            alias_lower="my wife", alias_label="my wife", canonical_label="Maria"))
    db.commit()

    r = client.delete(f"/v1/memories/{mem_public_id(m)}", params={"erase": "true"},
                      headers=_head(db, tenant, pid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "erased" and body["erasure"] == "permanent"
    assert body["facts_deleted"] == 1 and body["audit_id"].startswith("aud_")
    db.expire_all()
    assert db.get(AgentMemory, m) is None and db.get(AgentFact, f) is None
    job = db.get(AgentBatchJob, job.id)
    assert job.payload[0] == batch.ERASED_ITEM, "the payload kept the erased memory's text"
    assert job.payload[1]["content"] == "anna likes tea.", "only that item"
    assert job.last_error is None
    assert [a.alias_lower for a in db.query(AgentEntityAlias)] == ["my wife"], \
        "only the alias learned from the erased fact goes"
    assert db.get(PredicateCluster, rel.id).example_object is None, \
        "Anna has no other value, and Bruno's is in his own vocabulary, never hers"
    assert "diabetes" not in " ".join(vectors) and "asthma" not in " ".join(vectors)
    rec = db.query(AgentForgetAudit).filter(AgentForgetAudit.tenant_id == tenant.id).one()
    assert rec.scope == "memory" and rec.target_id == mem_public_id(m)


def test_a_memory_forgotten_earlier_can_be_erased(keyed, db, tenant, client):
    pid = seeded_project_id(db, tenant.id)
    m, f = _memory(db, tenant, pid, "anna", "asthma")
    head = _head(db, tenant, pid)
    path = f"/v1/memories/{mem_public_id(m)}"
    assert client.delete(path, headers=head).json()["status"] == "forgotten"
    assert client.delete(path, headers=head).status_code == 404, "a forget forgets once"
    r = client.delete(path, params={"erase": "true"}, headers=head)
    assert r.status_code == 200, r.text
    assert r.json()["erasure"] == "permanent" and r.json()["facts_deleted"] == 1
    db.expire_all()
    assert db.get(AgentMemory, m) is None and db.get(AgentFact, f) is None
    assert db.query(AgentForgetAudit).count() == 2, "the forget's record and the erasure's"
    assert client.delete(path, params={"erase": "true"}, headers=head).status_code == 404


def test_a_confined_key_erases_only_its_own_end_users(keyed, db, tenant, client):
    pid = seeded_project_id(db, tenant.id)
    m, _ = _memory(db, tenant, pid, "anna", "asthma")
    agent_facts.forget_memory(db, tenant.id, m, project_id=EVERY_PROJECT)
    head = _head(db, tenant, pid, scopes=["memories:read", "memories:write"], end_users=["bruno"])
    r = client.delete(f"/v1/memories/{mem_public_id(m)}", params={"erase": "true"}, headers=head)
    assert r.status_code == 404
    db.expire_all()
    assert db.get(AgentMemory, m) is not None


def test_another_projects_memory_is_not_erased(keyed, db, tenant, client):
    other = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="Pilot", slug="pilot")
    db.add(other)
    db.commit()
    m, _ = _memory(db, tenant, other.id, "anna", "asthma")
    r = client.delete(f"/v1/memories/{mem_public_id(m)}", params={"erase": "true"},
                      headers=_head(db, tenant, seeded_project_id(db, tenant.id)))
    assert r.status_code == 404
    db.expire_all()
    assert db.get(AgentMemory, m) is not None


def test_the_dashboard_erases_too(keyed, db, tenant, client):
    pid = seeded_project_id(db, tenant.id)
    m, f = _memory(db, tenant, pid, "anna", "asthma")
    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="admin",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    admin = {"Authorization": f"Bearer {full}"}
    path = f"/admin/memories/{mem_public_id(m)}"
    r = client.delete(path, headers=admin)
    assert r.status_code == 200 and r.json() == {"deleted": True, "id": mem_public_id(m)}
    db.expire_all()
    assert db.get(AgentMemory, m) is not None, "Delete forgets"
    r = client.delete(path, params={"erase": "true"}, headers=admin)
    assert r.status_code == 200 and r.json()["erasure"] == "permanent", r.text
    db.expire_all()
    assert db.get(AgentMemory, m) is None and db.get(AgentFact, f) is None


# ── a restore (services/erasure_replay.py) ─────────────────────────────────

def _restore(db, tenant, pid, memories):
    """The dump: the rows as they were, and no record of what came after."""
    db.query(AgentForgetAudit).delete()
    db.query(AgentMemory).filter(AgentMemory.id.in_([m for m, _f, _o in memories])).delete(
        synchronize_session=False)
    db.commit()
    db.expunge_all()
    for m, f, obj in memories:
        db.add(AgentMemory(id=m, tenant_id=tenant.id, project_id=pid, end_user_id="anna",
                           content=f"anna has {obj}.", graph_sync_status="done"))
        db.flush()
        db.add(AgentFact(id=f, tenant_id=tenant.id, project_id=pid, end_user_id="anna",
                         subject_label="anna", predicate_raw="has condition",
                         predicate_canonical="has_condition", object_label=obj,
                         source_memory_id=m, source_memory_ids=[str(m)]))
    db.commit()


def test_a_restore_erases_an_erased_memory_again(keyed, db, tenant):
    pid = seeded_project_id(db, tenant.id)
    erased, ef = _memory(db, tenant, pid, "anna", "asthma")
    forgotten, ff = _memory(db, tenant, pid, "anna", "flu")
    proof = agent_facts.erase_memory(db, tenant.id, erased, project_id=pid)["audit_id"]
    agent_facts.forget_memory(db, tenant.id, forgotten, project_id=pid)
    kept = erasure_replay.export(db)
    assert sorted(r["erased"] for r in kept) == [False, True]

    _restore(db, tenant, pid, [(erased, ef, "asthma"), (forgotten, ff, "flu")])
    out = erasure_replay.replay(db, kept)
    assert out["replayed"] == {"memory": 2}
    db.expire_all()
    assert db.get(AgentMemory, erased) is None and db.get(AgentFact, ef) is None, \
        "the erased memory came back as a forget"
    assert db.get(AgentMemory, forgotten).deleted_at is not None
    assert db.get(AgentFact, ff).invalid_at is not None
    from korely_agent.services.ids import audit_public_id
    assert proof in {audit_public_id(r.id) for r in db.query(AgentForgetAudit)}


def test_a_restore_erases_a_memory_erased_after_its_forget(keyed, db, tenant):
    """Forgotten before the dump, erased after it: the dump holds the forget's
    record and the row, forgotten; the erasure's record is not there."""
    pid = seeded_project_id(db, tenant.id)
    m, f = _memory(db, tenant, pid, "anna", "asthma")
    agent_facts.forget_memory(db, tenant.id, m, project_id=pid)
    before = {r.id for r in db.query(AgentForgetAudit)}
    agent_facts.erase_memory(db, tenant.id, m, project_id=pid)
    kept = erasure_replay.export(db)
    db.query(AgentForgetAudit).filter(AgentForgetAudit.id.notin_(before)).delete(
        synchronize_session=False)
    db.add(AgentMemory(id=m, tenant_id=tenant.id, project_id=pid, end_user_id="anna",
                       content="anna has asthma.", graph_sync_status="done",
                       deleted_at=datetime.now(timezone.utc)))
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["in_the_dump"] == 1 and out["replayed"] == {"memory": 1}
    db.expire_all()
    assert db.get(AgentMemory, m) is None


# ── an end user erased in one project ──────────────────────────────────────

def test_another_projects_records_keep_their_lists(keyed, db, tenant, client):
    from korely_agent.services import audit

    a = seeded_project_id(db, tenant.id)
    b = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="Beta", slug="beta")
    db.add(b)
    db.commit()
    audit.record(db, tenant.id, "key_create", actor="dashboard", project_id=b.id,
                 meta={"name": "b-support", "users": ["bob"]})
    audit.record(db, tenant.id, "key_create", actor="dashboard", project_id=a,
                 meta={"name": "a-support", "users": ["bob"]})
    audit.record(db, tenant.id, "key_create", actor="dashboard", project_id=a,
                 meta={"name": "wild", "users": ["b_b"]})
    r = client.delete("/v1/users/bob/memories", headers=_head(db, tenant, a))
    assert r.status_code == 200, r.text
    db.expire_all()
    metas = {e.meta["name"]: str(e.meta["users"]) for e in db.query(AuditEvent)
             .filter(AuditEvent.action == "key_create")}
    assert "bob" in metas["b-support"], "another project's record was rewritten"
    assert "bob" not in metas["a-support"]
    assert metas["wild"] == str(["b_b"])
    r = client.delete("/v1/users/b_b/memories", headers=_head(db, tenant, a))
    db.expire_all()
    metas = {e.meta["name"]: str(e.meta["users"]) for e in db.query(AuditEvent)
             .filter(AuditEvent.action == "key_create")}
    assert "bob" in metas["b-support"], "an `_` is a character, not a pattern"


def test_the_accounts_own_reads_take_the_pseudonym(keyed, db, tenant, client):
    """The screen's reads are filed with no project: a key's erasure left the
    id in clear there (audit of 18 Oct 2026)."""
    from korely_agent.services import operators
    from korely_agent.services.usage import log_v1_request

    a = seeded_project_id(db, tenant.id)
    b = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="Beta", slug="beta")
    db.add(b)
    db.commit()
    _memory(db, tenant, a, "anna", "asthma")
    _memory(db, tenant, a, "bob", "asthma")
    _memory(db, tenant, b.id, "bob", "flu")
    op, _pw = operators.create(db, tenant_id=tenant.id, username="op-dummy")
    for who in ("anna", "bob"):
        log_v1_request(db, tenant.id, "admin:memories:list", principal=op, end_user_id=who,
                       memories_read=[])
    head = _head(db, tenant, a)
    assert client.delete("/v1/users/anna/memories", headers=head).status_code == 200
    assert client.delete("/v1/users/bob/memories", headers=head).status_code == 200
    db.expire_all()
    screen = {e.end_user_id for e in db.query(AuditEvent).filter(AuditEvent.project_id.is_(None))}
    assert "anna" not in screen and pseudonyms.pseudonym(tenant.id, "anna") in screen
    assert "bob" in screen, "bob still lives in Beta: those reads may be of that person"


def test_an_ended_fact_another_memory_states_too_stays(keyed, db, tenant):
    """Audit of 19 Oct 2026: the erasure deleted by `source_memory_id`, and the
    forget's re-pointing skips ended facts, so an ended fact memory B states
    too (superseded, forgotten, closed) went with memory A, and B's history
    with it. Every fact naming A loses it, ended or not; only those A alone
    stated go."""
    pid = seeded_project_id(db, tenant.id)
    a, alone = _memory(db, tenant, pid, "maria", "asthma")
    b, _ = _memory(db, tenant, pid, "maria", "a cat")
    ended = datetime(2026, 4, 1, tzinfo=timezone.utc)

    def shared(obj, invalid_at):
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                      subject_label="maria", predicate_raw="lives in",
                      predicate_canonical="lives_in", object_label=obj, source_memory_id=a,
                      source_memory_ids=[str(a), str(b)], observation_count=2,
                      valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc), invalid_at=invalid_at)
        db.add(f)
        db.commit()
        return f.id

    was, now = shared("Turin", ended), shared("Milan", None)
    out = agent_facts.erase_memory(db, tenant.id, a, project_id=EVERY_PROJECT)

    assert out["facts_deleted"] == 1, out
    db.expire_all()
    assert db.get(AgentFact, alone) is None
    for fid in (was, now):
        f = db.get(AgentFact, fid)
        assert f is not None, "a fact memory B states went with memory A"
        assert f.source_memory_id == b and f.source_memory_ids == [str(b)]
        assert f.observation_count == 1
    assert db.get(AgentFact, was).invalid_at == ended, "the history is kept as it was"


def test_an_erased_rename_leaves_no_copy_of_the_new_name(keyed, db, tenant, client):
    """The code audit of 18 Oct 2026: erasing the memory that said
    "Acme is now Globex" deleted the alias in bulk, and "Globex" stayed in the
    canonical names of the facts kept and in the alias the rename had
    rewritten ("ACME Corp" -> "Acme" -> "Globex"). The alias is undone as the
    engine undoes one, and the chain it flattened is put back."""
    from korely_agent.services.facts import entities
    from korely_agent.services.memory_core import store
    from korely_agent.services.memory_core.identity import SOURCE_IDENTITY_FACT

    pid = seeded_project_id(db, tenant.id)
    entities.declare_alias(db, tenant.id, pid, "anna", None, "ACME Corp", "Acme")
    db.commit()
    kept = _memory(db, tenant, pid, "anna", "Slack")[1]
    db.query(AgentFact).filter(AgentFact.id == kept).update(
        {AgentFact.subject_label: "Acme", AgentFact.subject_canonical: "Acme",
         AgentFact.predicate_raw: "uses", AgentFact.predicate_canonical: "uses"})
    db.commit()
    m, rename = _memory(db, tenant, pid, "anna", "Globex")
    db.query(AgentFact).filter(AgentFact.id == rename).update(
        {AgentFact.subject_label: "Acme", AgentFact.subject_canonical: "Acme",
         AgentFact.predicate_raw: "renamed to", AgentFact.predicate_canonical: "renamed_to"})
    db.commit()
    store.add_alias(db, entities.TABLES, entities.memory_of(tenant.id, pid, "anna", None),
                            "Acme", "Globex", source=SOURCE_IDENTITY_FACT, fact_id=rename)
    db.commit()
    db.expire_all()
    assert db.get(AgentFact, kept).subject_canonical == "Globex"

    r = client.delete(f"/v1/memories/{mem_public_id(m)}", params={"erase": "true"},
                      headers=_head(db, tenant, pid))
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(AgentFact, kept).subject_canonical == "Acme"
    aliases = {(a.alias_lower, a.canonical_label) for a in db.query(AgentEntityAlias)
               .filter(AgentEntityAlias.tenant_id == tenant.id)}
    assert aliases == {("acme corp", "Acme")}, aliases
    facts = client.get("/v1/facts", headers=_head(db, tenant, pid), params={"user_id": "anna"}).text
    assert "Globex" not in facts
