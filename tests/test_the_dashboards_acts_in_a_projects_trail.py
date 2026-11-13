"""What the dashboard does in a project, in that project's trail (6 Nov 2026).

Since the data pages read the project chosen at the top, each read is filed
in that project's trail, where its keys read GET /v1/audit: the row stays
there, which an erasure and the trail's completeness need, but a key reads
it without the name the operator signs in with (`meta.operator`), which
/admin/audit keeps. The Home page's count reads no memory and files nothing.

The dashboard's acts on a fact (forget, correct) and its reading of an
event's ids (/admin/audit/read) were filed with no project and no end user,
so the project's trail did not list them and the trail asked for the end
user did not find them: they are filed with the project and the end user of
what they touched, as the forget of a memory already was (review of 6 Nov 2026).
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentFact, AgentMemory, AuditEvent
from korely_agent.services import keys as key_service
from korely_agent.services import operators, projects
from korely_agent.services.ids import fact_public_id, mem_public_id

OPERATOR = "maria.rossi@agency.example"


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
def screen(db, tenant):
    op, _password = operators.create(db, tenant_id=tenant.id, username=OPERATOR)
    _row, token = operators.open_session(db, op, ip="203.0.113.7")
    return {"Authorization": f"Bearer {token}"}


def _key(db, tenant_id, project):
    _r, full = key_service.create_api_key(db, tenant_id=tenant_id, name=f"k-{uuid.uuid4().hex[:6]}",
                                          project_id=project.id)
    return {"Authorization": f"Bearer {full}"}


def _fill(db, tenant_id, project, who):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant_id, project_id=project.id,
                    end_user_id=who, agent_id="support-bot", content=f"{who} prefers email")
    db.add(m)
    db.flush()
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant_id, project_id=project.id, end_user_id=who,
                  agent_id="support-bot", subject_label=who, predicate_raw="prefers",
                  predicate_canonical="prefers", object_label="email", source_memory_id=m.id,
                  valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc))
    db.add(f)
    db.commit()
    return m, f


@pytest.fixture
def two(db, tenant):
    a = projects.create_project(db, tenant_id=tenant.id, name="A")
    b = projects.create_project(db, tenant_id=tenant.id, name="B")
    return a, b, _fill(db, tenant.id, a, "anna"), _fill(db, tenant.id, b, "bruno")


def _dashboard_rows(trail):
    return [e for e in trail["events"] if e["actor"] == "dashboard"]


def test_a_project_key_reads_the_screens_reads_without_the_operators_name(db, tenant, client,
                                                                          screen, two):
    a, _b, (m, f), _ = two
    for path in ("/admin/memories", "/admin/facts"):
        assert client.get(path, params={"project_id": str(a.id)}, headers=screen).status_code == 200
    trail = client.get("/v1/audit", headers=_key(db, tenant.id, a)).json()
    rows = _dashboard_rows(trail)
    assert {r["meta"]["endpoint"] for r in rows} >= {"admin:memories:list", "admin:facts:list"}
    assert OPERATOR not in str(trail), "a key of the project reads the operator's sign-in name"
    assert all("operator" not in (r["meta"] or {}) for r in trail["events"])
    # The rows stay, with what they read.
    listed = next(r for r in rows if r["meta"]["endpoint"] == "admin:memories:list")
    assert listed["read"]["memories"] == [mem_public_id(m.id)]
    # The account's own screen keeps the name.
    mine = client.get("/admin/audit", params={"project_id": str(a.id)}, headers=screen).json()
    assert {e["meta"].get("operator") for e in _dashboard_rows(mine)} == {OPERATOR}


def test_the_home_pages_count_files_nothing(db, tenant, client, screen, two):
    a, _b, _a, _b2 = two
    before = db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id).count()
    r = client.get("/admin/memories/count", params={"project_id": str(a.id)}, headers=screen)
    assert r.json() == {"total": 1}
    db.expire_all()
    assert db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id).count() == before


@pytest.mark.parametrize("act", ["forget", "correct"])
def test_an_act_on_a_fact_is_filed_with_its_project_and_end_user(db, tenant, client, screen,
                                                                 two, act):
    a, _b, (_m, f), _ = two
    fid = fact_public_id(f.id)
    r = (client.post(f"/admin/facts/{fid}/forget", headers=screen) if act == "forget"
         else client.patch(f"/admin/facts/{fid}", headers=screen, json={"object": "phone"}))
    assert r.status_code == 200, r.text
    db.expire_all()
    row = (db.query(AuditEvent)
             .filter(AuditEvent.tenant_id == tenant.id, AuditEvent.target_id == fid)
             .one())
    assert (row.project_id, row.end_user_id) == (a.id, "anna")
    # So the project's trail lists it, and its keys read it.
    shown = client.get("/admin/audit", params={"project_id": str(a.id)}, headers=screen).json()
    assert fid in {e["target_id"] for e in shown["events"]}
    seen = client.get("/v1/audit", headers=_key(db, tenant.id, a)).json()
    assert fid in {e["target_id"] for e in seen["events"]}


def test_reading_an_events_ids_is_filed_where_each_of_them_lives(db, tenant, client, screen,
                                                                 two):
    a, b, (ma, fa), (mb, _fb) = two
    r = client.get("/admin/audit/read", headers=screen,
                   params={"memories": f"{mem_public_id(ma.id)},{mem_public_id(mb.id)}",
                           "facts": fact_public_id(fa.id)})
    assert r.status_code == 200
    db.expire_all()
    rows = (db.query(AuditEvent)
              .filter(AuditEvent.tenant_id == tenant.id,
                      AuditEvent.meta["endpoint"].as_string() == "admin:audit:read").all())
    filed = {(r.project_id, r.end_user_id): r.read_ids for r in rows}
    assert filed == {
        (a.id, "anna"): {"memories": [mem_public_id(ma.id)], "facts": [fact_public_id(fa.id)]},
        (b.id, "bruno"): {"memories": [mem_public_id(mb.id)]},
    }


# The class, not only the two acts named (korely-fix-alla-radice): every act
# of the screen on a row that lives in a project is filed in that project, and
# with its end user when the row has one. A key's creation and its move were;
# its revocation, a webhook's creation, deletion and enabling, and an alias's
# creation and deletion were filed under no project.

def _filed(db, tenant_id, target):
    db.expire_all()
    rows = (db.query(AuditEvent)
              .filter(AuditEvent.tenant_id == tenant_id, AuditEvent.target_id == target,
                      AuditEvent.action != "read").all())
    return {(r.action, r.project_id, r.end_user_id) for r in rows}


def test_a_keys_revocation_is_filed_in_its_project(db, tenant, client, screen, two):
    a, _b, _a, _b2 = two
    made = client.post("/admin/keys", headers=screen,
                       json={"name": "shop-agent", "project_id": str(a.id)}).json()
    assert client.post(f"/admin/keys/{made['id']}/revoke", headers=screen).status_code == 200
    assert _filed(db, tenant.id, made["id"]) == {("key_create", a.id, None),
                                                 ("key_revoke", a.id, None)}


def test_a_webhooks_life_is_filed_in_its_project(db, tenant, client, screen, two, monkeypatch):
    from korely_agent.services import webhooks

    monkeypatch.setattr(webhooks, "check_url", lambda u: u.strip())
    a, _b, _a, _b2 = two
    made = client.post("/admin/webhooks", headers=screen,
                       json={"url": "https://hooks.example/a", "project_id": str(a.id)}).json()
    every = client.post("/admin/webhooks", headers=screen,
                        json={"url": "https://hooks.example/all"}).json()
    assert client.delete(f"/admin/webhooks/{made['id']}", headers=screen).status_code == 200
    assert _filed(db, tenant.id, made["id"]) == {("setting_change", a.id, None)}
    assert len(db.query(AuditEvent).filter(AuditEvent.target_id == made["id"]).all()) == 2
    # An endpoint of every project is the account's, under no project.
    assert _filed(db, tenant.id, every["id"]) == {("setting_change", None, None)}


def test_an_aliass_life_is_filed_in_its_project_with_its_end_user(db, tenant, client, screen,
                                                                  two):
    from korely_agent.services.facts import service

    a, _b, _a, _b2 = two
    service.write_fact_direct(db, tenant.id, subject="Acme", predicate="based_in", obj="Milan",
                              end_user_id="anna", subject_type="organization", project_id=a.id)
    r = client.post("/admin/aliases", headers=screen,
                    json={"alias": "Acme", "canonical": "Globex", "user_id": "anna",
                          "project_id": str(a.id)})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    assert client.delete(f"/admin/aliases/{aid}", headers=screen).status_code == 200
    assert _filed(db, tenant.id, aid) == {("setting_change", a.id, "anna")}
    assert len(db.query(AuditEvent).filter(AuditEvent.target_id == aid).all()) == 2


def test_a_webhooks_enabling_and_its_test_are_filed_in_its_project(db, tenant, client, screen,
                                                                   two, monkeypatch):
    """The enabling (S3 of the review of 6 Nov 2026), and the test delivery, which
    sent a delivery and filed nothing: in the endpoint's project, with
    whether it was delivered, never the address."""
    from korely_agent.services import webhooks

    monkeypatch.setattr(webhooks, "check_url", lambda u: u.strip())
    monkeypatch.setattr(webhooks, "_deliver", lambda url, secret, did, payload: (True, 204, None))
    a, _b, _a, _b2 = two
    made = client.post("/admin/webhooks", headers=screen,
                       json={"url": "https://hooks.example/a?token=s3cret",
                             "project_id": str(a.id)}).json()
    assert client.post(f"/admin/webhooks/{made['id']}/enable", headers=screen).status_code == 200
    tested = client.post(f"/admin/webhooks/{made['id']}/test", headers=screen)
    assert tested.status_code == 200 and tested.json()["delivered"] is True
    db.expire_all()
    rows = db.query(AuditEvent).filter(AuditEvent.target_id == made["id"]).all()
    ops = {r.meta["op"]: r for r in rows}
    assert set(ops) == {"create", "enable", "test"}
    assert {r.project_id for r in rows} == {a.id}
    assert (ops["test"].meta["delivered"], ops["test"].meta["status"]) == (True, 204)
    assert "s3cret" not in str([r.meta for r in rows]) and "hooks.example" not in str(
        [r.meta for r in rows])


def test_a_correction_refused_by_the_quota_is_filed_with_its_project_and_end_user(
        db, tenant, client, screen, two, monkeypatch):
    """S5 of the review of 6 Nov 2026: the Cloud's monthly quota refuses a correction
    (core/extensions.hooks.before_write); the refusal in the trail carries
    the fact's project and end user, as the correction would."""
    from fastapi import HTTPException

    from korely_agent.core.extensions import hooks

    def over(*a, **k):
        raise HTTPException(429, {"code": "quota_exceeded", "message": "month used"})

    monkeypatch.setattr(hooks, "before_write", over)
    a, _b, (_m, f), _ = two
    fid = fact_public_id(f.id)
    r = client.patch(f"/admin/facts/{fid}", headers=screen, json={"object": "phone"})
    assert r.status_code == 429
    db.expire_all()
    (row,) = db.query(AuditEvent).filter(AuditEvent.target_id == fid).all()
    assert (row.result, row.meta["code"], row.project_id, row.end_user_id) == (
        "error", "quota_exceeded", a.id, "anna")
