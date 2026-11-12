"""What an erasure leaves of the end user it erased (0.1.27, the legal review
of 11 Oct 2026): a keyed pseudonym in place of their id in
the erasure record, the access trail and the delivered webhooks; no end user
on the cost rows; the webhooks not delivered yet as they were. The developer
still finds the proof by the id (GET /v1/audit?user_id=)."""
import uuid

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import (AgentForgetAudit, AgentMemory,
                                    AgentWebhookDelivery, AgentWebhookEndpoint, AuditEvent)
from korely_agent.services import keys as key_service
from korely_agent.services import pseudonyms, vectors
from korely_agent.services.usage import log_v1_request
from tests.conftest import seeded_project_id
from tests.thread_helpers import wait_idle



@pytest.fixture
def keyed(monkeypatch):
    from korely_agent.config import get_settings

    monkeypatch.setenv("SECRET_KEY", "a-long-random-string-for-the-tests")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── the pseudonym ───────────────────────────────────────────────────────────

def test_the_pseudonym_is_keyed_and_per_account(keyed, monkeypatch):
    from korely_agent.config import get_settings

    t1, t2 = uuid.uuid4(), uuid.uuid4()
    p = pseudonyms.pseudonym(t1, "maria@example.com")
    assert p.startswith("erased:") and "maria" not in p
    assert pseudonyms.pseudonym(t1, "maria@example.com") == p
    assert pseudonyms.pseudonym(t2, "maria@example.com") != p, "two accounts never cross"
    monkeypatch.setenv("SECRET_KEY", "another-secret")
    get_settings.cache_clear()
    assert pseudonyms.pseudonym(t1, "maria@example.com") != p, "another key, another pseudonym"
    monkeypatch.delenv("SECRET_KEY")
    get_settings.cache_clear()
    assert pseudonyms.pseudonym(t1, "maria@example.com") is None


def test_the_migration_computes_the_same_pseudonym(keyed):
    """The migration of the past erasures carries its own copy of the scheme."""
    import importlib.util
    from pathlib import Path

    path = next(Path(__file__).resolve().parent.parent.glob(
        "alembic/versions/a5d81f3c7e64_*.py"))
    spec = importlib.util.spec_from_file_location("mig_pseudonyms", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    t = uuid.uuid4()
    assert mig.pseudonym(mig._key(), t, "maria") == pseudonyms.pseudonym(t, "maria")


# ── an erasure ──────────────────────────────────────────────────────────────

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


@pytest.fixture
def world(db, tenant):
    """Maria's memory, a read about her, an earlier forget of hers, and three
    deliveries: one about her made, one about her still to make, one about
    somebody else made. A model call she caused names nobody (audit of 19 Oct
    2026, tests/test_llm_usage.py)."""
    project = seeded_project_id(db, tenant.id)
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project, end_user_id="maria",
                    content="user: I live in Milan.", embedding=[0.1] * EMBEDDING_DIM,
                    embedding_status=vectors.DONE, graph_sync_status="done")
    db.add(m)
    db.add(AgentForgetAudit(tenant_id=tenant.id, end_user_id="maria", project_id=project,
                            scope="memory", target_id="mem_x", memories_forgotten=1))
    ep = AgentWebhookEndpoint(id=uuid.uuid4(), tenant_id=tenant.id, url="https://hooks.example/x",
                              secret="whsec_t", events=["memory.created"], active=True)
    db.add(ep)
    db.flush()

    def delivery(user, status):
        d = AgentWebhookDelivery(tenant_id=tenant.id, endpoint_id=ep.id, event_type="memory.created",
                                 payload={"event": "memory.created",
                                          "data": {"id": "mem_1", "user_id": user, "agent_id": None},
                                          "project_id": str(project) if project else None},
                                 status=status)
        db.add(d)
        return d

    made, waiting, other = delivery("maria", "delivered"), delivery("maria", "pending"), \
        delivery("luca", "delivered")
    db.commit()
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name="agent",
                                          project_id=project, expires_in_days=None)
    log_v1_request(db, tenant.id, "context", end_user_id="maria", api_key_id=_r.id)
    return {"project": project, "head": {"Authorization": f"Bearer {full}"},
            "made": made.id, "waiting": waiting.id, "other": other.id}


def _ids_left(db, tenant):
    db.expire_all()
    return {
        "records": {r.end_user_id for r in db.query(AgentForgetAudit)
                    .filter(AgentForgetAudit.tenant_id == tenant.id)},
        "trail": {r.end_user_id for r in db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id)},
    }


def test_an_erasure_leaves_a_pseudonym_and_no_id(keyed, db, tenant, client, world):
    r = client.delete("/v1/users/maria/memories", headers=world["head"])
    assert r.status_code == 200, r.text
    p = pseudonyms.pseudonym(tenant.id, "maria")
    left = _ids_left(db, tenant)
    assert left["records"] == {p}, "the earlier forget and the erasure's own record"
    assert "maria" not in left["trail"] and p in left["trail"]
    made, waiting, other = (db.get(AgentWebhookDelivery, world[k]) for k in ("made", "waiting", "other"))
    assert made.payload["data"]["user_id"] == p
    assert waiting.payload["data"]["user_id"] == "maria", "not delivered yet: goes out as enqueued"
    assert other.payload["data"]["user_id"] == "luca"


def test_the_proof_is_found_by_the_id(keyed, db, tenant, client, world):
    client.delete("/v1/users/maria/memories", headers=world["head"])
    r = client.get("/v1/audit", params={"user_id": "maria"}, headers=world["head"])
    assert r.status_code == 200, r.text
    events = r.json()["events"]
    assert any(e["action"] == "erase" for e in events)
    assert all(e["user_id"] == pseudonyms.pseudonym(tenant.id, "maria") for e in events)


def test_without_a_key_the_id_goes_and_nothing_replaces_it(db, tenant, client, world):
    r = client.delete("/v1/users/maria/memories", headers=world["head"])
    assert r.status_code == 200, r.text
    left = _ids_left(db, tenant)
    assert left["records"] == {None} and "maria" not in left["trail"]
    assert r.json()["audit_id"].startswith("aud_"), "the proof is the audit id"


def test_an_erased_project_leaves_its_end_users_a_pseudonym(keyed, db, tenant, world):
    from korely_agent.db.models import AgentProject
    from korely_agent.services import projects

    other = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="Pilot", slug="pilot")
    db.add(other)
    db.flush()
    db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=other.id, end_user_id="anna",
                       content="user: hi", embedding=[0.1] * EMBEDDING_DIM,
                       embedding_status=vectors.DONE, graph_sync_status="done"))
    db.commit()
    log_v1_request(db, tenant.id, "context", end_user_id="anna", project_id=other.id)
    projects.delete_project(db, tenant.id, db.get(AgentProject, other.id), erase=True)
    trail = _ids_left(db, tenant)["trail"]
    assert "anna" not in trail and pseudonyms.pseudonym(tenant.id, "anna") in trail
    assert "maria" in trail, "the other project's end user is not erased"


# ── the pseudonym is nobody's id (audit of 17 Oct 2026) ─────────────────────

def test_the_pseudonyms_prefix_is_no_id_and_no_confinement():
    from korely_agent.services import user_scope
    from korely_agent.services.ids import namespace_id_problem

    assert namespace_id_problem("erased:0123456789abcdef0123456789abcdef")
    assert namespace_id_problem("erased:anything")
    with pytest.raises(ValueError):
        user_scope.checked(["erased:*"])
    assert not user_scope.allows(["e*"], "erased:0123456789abcdef0123456789abcdef")


def test_a_confined_key_does_not_read_other_peoples_erasure_records(keyed, db, tenant, client,
                                                                    world):
    """A key confined to 'e*' listed the pseudonymized rows of zoe's erasure."""
    from korely_agent.services import keys as key_service

    assert client.delete("/v1/users/maria/memories", headers=world["head"]).status_code == 200
    _r, confined = key_service.create_api_key(db, tenant_id=tenant.id, name="e-only",
                                              scopes=["memories:read", "memories:write"],
                                              project_id=seeded_project_id(db, tenant.id),
                                              end_users=["e*"])
    rows = client.get("/v1/audit", headers={"Authorization": f"Bearer {confined}"}).json()
    events = rows.get("events", rows.get("items", []))
    assert not any(str(e.get("user_id", "")).startswith("erased:") for e in events)


def test_an_erasure_record_cannot_be_hidden_by_erasing_its_pseudonym(keyed, db, tenant, client,
                                                                      world):
    assert client.delete("/v1/users/maria/memories", headers=world["head"]).status_code == 200
    p = pseudonyms.pseudonym(tenant.id, "maria")
    r = client.delete(f"/v1/users/{p}/memories", headers=world["head"])
    assert r.status_code == 422
    r = client.post("/v1/memories", headers=world["head"],
                    json={"content": "forged", "user_id": p})
    assert r.status_code == 422


def test_a_delivery_that_finishes_after_the_erasure_takes_the_pseudonym(keyed, db, tenant,
                                                                        client, world, monkeypatch):
    """The waiting delivery goes out with the id its receiver knows; once it is
    delivered, the row keeps the pseudonym, as the finished ones did at the
    erasure (audit of 17 Oct 2026: it kept the id for 30 days)."""
    from korely_agent.services import webhooks

    assert client.delete("/v1/users/maria/memories", headers=world["head"]).status_code == 200
    sent = []
    monkeypatch.setattr(webhooks, "_deliver",
                        lambda url, secret, did, payload: sent.append(payload) or (True, 200, None))
    webhooks.process_deliveries(db)
    # The outcome is written by the attempt's own thread, which on a loaded
    # machine may still run when the round returns.
    assert wait_idle(webhooks._sending, 30)
    p = pseudonyms.pseudonym(tenant.id, "maria")
    assert sent and sent[0]["data"]["user_id"] == "maria", "it went out as it was enqueued"
    db.expire_all()
    waiting = db.get(AgentWebhookDelivery, world["waiting"])
    assert waiting.status == "delivered" and waiting.payload["data"]["user_id"] == p


def test_a_delivery_dropped_after_the_erasure_takes_the_pseudonym(keyed, db, tenant, client,
                                                                   world, monkeypatch):
    """Its endpoint switched off, the waiting delivery is dropped unsent when it
    falls due: finished, it kept the id for the 30 days of retention (audit
    of 18 Oct 2026)."""
    from korely_agent.services import webhooks

    assert client.delete("/v1/users/maria/memories", headers=world["head"]).status_code == 200
    db.query(AgentWebhookEndpoint).update({AgentWebhookEndpoint.active: False})
    db.commit()
    monkeypatch.setattr(webhooks, "_deliver", lambda *a: pytest.fail("never sent"))
    assert webhooks.process_deliveries(db)["failed"] == 1
    db.expire_all()
    waiting = db.get(AgentWebhookDelivery, world["waiting"])
    assert waiting.status == "failed" and waiting.attempts == waiting.max_attempts
    assert waiting.payload["data"]["user_id"] == pseudonyms.pseudonym(tenant.id, "maria")


def test_a_delivery_of_an_erased_project_takes_the_pseudonym_when_it_finishes(
        keyed, db, tenant, world, monkeypatch):
    """An erased project files its erasure with scope 'project' and no end
    user: a delivery of it still waiting, to an endpoint of every project,
    finished with the id in clear (audit of 18 Oct 2026)."""
    from korely_agent.db.models import AgentProject
    from korely_agent.services import projects, webhooks

    pilot = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="Pilot", slug="pilot")
    db.add(pilot)
    db.flush()
    db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pilot.id, end_user_id="anna",
                       content="user: hi", embedding=[0.1] * EMBEDDING_DIM,
                       embedding_status=vectors.DONE, graph_sync_status="done"))
    ep = db.query(AgentWebhookEndpoint).one()
    d = AgentWebhookDelivery(tenant_id=tenant.id, endpoint_id=ep.id, event_type="memory.created",
                             payload={"event": "memory.created",
                                      "data": {"id": "mem_2", "user_id": "anna", "agent_id": None},
                                      "project_id": str(pilot.id)},
                             status="pending")
    db.add(d)
    db.commit()
    projects.delete_project(db, tenant.id, db.get(AgentProject, pilot.id), erase=True)
    db.commit()
    sent = []
    monkeypatch.setattr(webhooks, "_deliver",
                        lambda url, secret, did, payload: sent.append(payload) or (True, 200, None))
    webhooks.process_deliveries(db)
    # As above: the attempts write their outcomes in their own threads.
    assert wait_idle(webhooks._sending, 30)
    assert [x["data"]["user_id"] for x in sent if x["data"]["id"] == "mem_2"] == ["anna"]
    db.expire_all()
    assert db.get(AgentWebhookDelivery, d.id).payload["data"]["user_id"] == \
        pseudonyms.pseudonym(tenant.id, "anna")
    assert db.get(AgentWebhookDelivery, world["waiting"]).payload["data"]["user_id"] == "maria", \
        "an end user of another project, not erased"


def test_a_delivery_already_spent_takes_the_pseudonym_at_the_erasure(keyed, db, tenant, client,
                                                                     world):
    spent = db.get(AgentWebhookDelivery, world["waiting"])
    spent.status, spent.attempts, spent.max_attempts = "failed", 6, 6
    db.commit()
    assert client.delete("/v1/users/maria/memories", headers=world["head"]).status_code == 200
    db.expire_all()
    assert db.get(AgentWebhookDelivery, world["waiting"]).payload["data"]["user_id"] == \
        pseudonyms.pseudonym(tenant.id, "maria")


def test_a_key_confined_to_the_erased_end_user_keeps_no_id(keyed, db, tenant, client, world):
    """A key confined to maria kept her id in its list for ever, and the audit
    row of its creation in meta.users (audit of 17 Oct 2026)."""
    from korely_agent.db.models import AgentApiKey
    from korely_agent.services import audit

    pid = seeded_project_id(db, tenant.id)
    only, _ = key_service.create_api_key(db, tenant_id=tenant.id, name="only-one",
                                         scopes=["memories:read"], project_id=pid,
                                         end_users=["maria"])
    both, _ = key_service.create_api_key(db, tenant_id=tenant.id, name="two",
                                         scopes=["memories:read"], project_id=pid,
                                         end_users=["maria", "luca", "acme:*"])
    audit.record(db, tenant.id, "key_create", actor="dashboard", project_id=pid,
                 meta={"name": "only-one", "users": ["maria"]})
    assert client.delete("/v1/users/maria/memories", headers=world["head"]).status_code == 200
    db.expire_all()
    p = pseudonyms.pseudonym(tenant.id, "maria")
    only, both = db.get(AgentApiKey, only.id), db.get(AgentApiKey, both.id)
    assert only.end_users == [p] and only.revoked_at is not None, "a key that sees nobody"
    assert both.end_users == [p, "luca", "acme:*"] and both.revoked_at is None
    metas = [e.meta for e in db.query(AuditEvent).filter(AuditEvent.action == "key_create")]
    assert metas and not any("maria" in str(m) for m in metas)


def test_checking_the_erasure_does_not_write_the_id_back(keyed, db, tenant, client, world):
    """The README's check after an erasure, GET /v1/facts?user_id=<the id>,
    wrote the id in clear in the trail, next to the pseudonymized rows (audit
    of 17 Oct 2026)."""
    assert client.delete("/v1/users/maria/memories", headers=world["head"]).status_code == 200
    r = client.get("/v1/facts", params={"user_id": "maria", "include_invalidated": "true"},
                   headers=world["head"])
    assert r.status_code == 200 and r.json()["facts"] == []
    db.expire_all()
    assert "maria" not in {e.end_user_id for e in db.query(AuditEvent)}
    r = client.get("/v1/audit", params={"user_id": "maria"}, headers=world["head"])
    assert r.status_code == 200 and r.json()["events"], "the proof is still found by the id"


def test_the_migration_reads_the_key_where_the_application_does(monkeypatch):
    """Run by hand from a checkout, with SECRET_KEY only in ./.env, the
    migration found no key and wrote NULL over every past erasure's id (audit
    of 17 Oct 2026)."""
    import importlib.util
    from pathlib import Path

    from korely_agent import config

    path = next(Path(__file__).resolve().parent.parent.glob("alembic/versions/a5d81f3c7e64_*.py"))
    spec = importlib.util.spec_from_file_location("mig_pseudonyms_env", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    monkeypatch.delenv("SECRET_KEY", raising=False)

    class FromTheFile:
        secret_key = "only-in-the-dot-env-file"

    monkeypatch.setattr(config, "get_settings", lambda: FromTheFile())
    assert mig._key() is not None


def test_one_memory_can_be_erased_for_good(keyed, db, tenant, client, world):
    """DELETE /v1/memories/{id} forgot the memory and kept its text,
    observation and vector for ever; only a whole end user could be erased
    (audit of 17 Oct 2026). With erase=true the row goes."""
    from korely_agent.services.ids import mem_public_id

    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                    end_user_id="luca", content="Luca's card ends in 4242.", graph_sync_status="done")
    db.add(m)
    db.commit()
    mid = m.id
    r = client.delete(f"/v1/memories/{mem_public_id(mid)}", params={"erase": "true"},
                      headers=world["head"])
    assert r.status_code == 200 and r.json()["erasure"] == "permanent", r.text
    db.expire_all()
    assert db.get(AgentMemory, mid) is None
    assert db.query(AgentForgetAudit).filter(AgentForgetAudit.scope == "memory",
                                             AgentForgetAudit.end_user_id == "luca").count() == 1


def test_a_forgotten_memorys_text_is_not_read_back_on_the_screen(keyed, db, tenant, client, world):
    from korely_agent.services.ids import mem_public_id

    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                    end_user_id="luca", content="Luca's card ends in 4242.", graph_sync_status="done")
    db.add(m)
    db.commit()
    assert client.delete(f"/v1/memories/{mem_public_id(m.id)}", headers=world["head"]).status_code == 200
    _r, admin = key_service.create_api_key(db, tenant_id=tenant.id, name="adm",
                                           scopes=["memories:read", "admin"])
    r = client.get("/admin/audit/read", params={"memories": mem_public_id(m.id)},
                   headers={"Authorization": f"Bearer {admin}"})
    assert r.status_code == 200, r.text
    assert "4242" not in r.text and r.json()["memories"][mem_public_id(m.id)]["deleted"] is True
