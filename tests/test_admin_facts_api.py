"""Forgetting and correcting, which must not delete anything.

This is the one place a screen lets somebody change the store by hand, and it
is the place where the product would quietly stop being itself. If forgetting
removed a row, "what did this system believe in March" would become
unanswerable for every fact anyone had ever tidied up, and the answer would
still look confident. So the test is not that the fact disappears from the
current state. It is that it is still there on the first of April.
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tests.conftest import seeded_project_id
from korely_agent.db.models import AgentFact, Tenant
from korely_agent.services import keys as key_service


D = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


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
        db, tenant_id=tenant.id, name="operator",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    return {"Authorization": f"Bearer {full}"}


@pytest.fixture
def agent_key(db, tenant):
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name="agent")
    return {"Authorization": f"Bearer {full}"}


def put(db, tenant_id, subject="Maria", predicate="is_on", obj="Priority plan",
        since="2026-03-15", eu="c4821"):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant_id, end_user_id=eu,
                  project_id=seeded_project_id(db, tenant_id),
                  subject_label=subject, predicate_raw=predicate.replace("_", " "),
                  predicate_canonical=predicate, object_label=obj,
                  valid_from=D(since))
    db.add(f)
    db.commit()
    return f


def public(fact) -> str:
    from korely_agent.services.ids import fact_public_id

    return fact_public_id(fact.id)


class TestForgetting:
    def test_it_leaves_the_past_readable(self, db, tenant, client, admin, agent_key):
        """The whole product in one assertion."""
        f = put(db, tenant.id, since="2026-03-15")

        assert client.post(f"/admin/facts/{public(f)}/forget", headers=admin).status_code == 200

        now = client.get("/v1/facts?user_id=c4821", headers=agent_key).json()
        assert now["facts"] == [], "it is still in the current state"

        then = client.get("/v1/facts?user_id=c4821&as_of=2026-04-01",
                          headers=agent_key).json()
        assert len(then["facts"]) == 1, "April can no longer see what was true in April"
        assert then["facts"][0]["object"] == "Priority plan"

    def test_the_row_is_still_there_with_a_closing_date(self, db, tenant, client, admin):
        f = put(db, tenant.id)
        client.post(f"/admin/facts/{public(f)}/forget", headers=admin)
        db.expire_all()

        row = db.query(AgentFact).filter(AgentFact.id == f.id).one_or_none()
        assert row is not None, "the fact was deleted"
        assert row.invalid_at is not None

    def test_forgetting_twice_says_so_rather_than_moving_the_date(self, db, tenant, client, admin):
        f = put(db, tenant.id)
        first = client.post(f"/admin/facts/{public(f)}/forget", headers=admin).json()
        assert first["status"] == "forgotten"
        db.expire_all()
        closed_at = db.query(AgentFact).filter(AgentFact.id == f.id).one().invalid_at

        second = client.post(f"/admin/facts/{public(f)}/forget", headers=admin).json()
        assert second["status"] == "already_forgotten"
        db.expire_all()
        assert db.query(AgentFact).filter(AgentFact.id == f.id).one().invalid_at == closed_at, \
            "the second call moved the day it stopped being true"

    def test_it_leaves_an_audit_row(self, db, tenant, client, admin):
        from korely_agent.db.models import AgentForgetAudit

        f = put(db, tenant.id)
        out = client.post(f"/admin/facts/{public(f)}/forget", headers=admin).json()
        assert out["audit_id"].startswith("aud_")
        assert db.query(AgentForgetAudit).filter(
            AgentForgetAudit.tenant_id == tenant.id).count() == 1

    def test_an_agent_key_cannot(self, db, tenant, client, agent_key):
        f = put(db, tenant.id)
        assert client.post(f"/admin/facts/{public(f)}/forget",
                           headers=agent_key).status_code == 403

    def test_another_tenant_cannot(self, db, tenant, client, admin):
        other = Tenant(id=uuid.uuid4(), name="Somebody Else")
        db.add(other)
        db.commit()
        theirs = put(db, other.id)

        assert client.post(f"/admin/facts/{public(theirs)}/forget",
                           headers=admin).status_code == 404
        db.expire_all()
        assert db.query(AgentFact).filter(AgentFact.id == theirs.id).one().invalid_at is None

    def test_rubbish_id_is_a_404(self, client, admin):
        assert client.post("/admin/facts/not-an-id/forget", headers=admin).status_code == 404


class TestCorrecting:
    def test_it_supersedes_instead_of_editing(self, db, tenant, client, admin, agent_key):
        f = put(db, tenant.id, obj="Priority plan", since="2026-03-15")

        out = client.patch(f"/admin/facts/{public(f)}", headers=admin,
                           json={"object": "Basic plan"}).json()
        assert out["object"] == "Basic plan"
        db.expire_all()

        old = db.query(AgentFact).filter(AgentFact.id == f.id).one()
        assert old.object_label == "Priority plan", "the original row was edited"
        assert old.invalid_at is not None
        assert old.invalidated_by_id is not None, "nothing says what replaced it"

        then = client.get("/v1/facts?user_id=c4821&as_of=2026-04-01",
                          headers=agent_key).json()
        assert then["facts"][0]["object"] == "Priority plan"

    def test_an_empty_correction_is_refused(self, db, tenant, client, admin):
        f = put(db, tenant.id)
        r = client.patch(f"/admin/facts/{public(f)}", headers=admin, json={})
        assert r.status_code == 422
        assert "at least one" in r.json()["detail"]

    def test_whitespace_is_not_a_correction(self, db, tenant, client, admin):
        f = put(db, tenant.id)
        assert client.patch(f"/admin/facts/{public(f)}", headers=admin,
                            json={"object": "   "}).status_code == 422

    def test_another_tenant_cannot(self, db, tenant, client, admin):
        other = Tenant(id=uuid.uuid4(), name="Somebody Else")
        db.add(other)
        db.commit()
        theirs = put(db, other.id)
        assert client.patch(f"/admin/facts/{public(theirs)}", headers=admin,
                            json={"object": "x"}).status_code == 404


class TestTheRestOfTheScreen:
    def test_moving_a_key_between_projects(self, db, tenant, client, admin):
        made = client.post("/admin/keys", headers=admin, json={"name": "k"}).json()
        proj = client.post("/admin/projects", headers=admin, json={"name": "Prod"}).json()

        moved = client.post(f"/admin/keys/{made['id']}/move", headers=admin,
                            json={"project_id": proj["id"]}).json()
        assert moved["project_id"] == proj["id"]

        back = client.post(f"/admin/keys/{made['id']}/move", headers=admin,
                           json={"project_id": None}).json()
        assert back["project_id"] is None

    def test_moving_into_a_project_that_is_not_yours(self, db, tenant, client, admin):
        other = Tenant(id=uuid.uuid4(), name="Somebody Else")
        db.add(other)
        db.commit()
        from korely_agent.services import projects as project_service

        theirs = project_service.create_project(db, tenant_id=other.id, name="Theirs")
        made = client.post("/admin/keys", headers=admin, json={"name": "k"}).json()

        r = client.post(f"/admin/keys/{made['id']}/move", headers=admin,
                        json={"project_id": str(theirs.id)})
        assert r.status_code == 404

    def test_a_test_delivery_reports_what_came_back(self, db, tenant, client, admin, monkeypatch):
        from korely_agent.services import webhooks

        seen = {}

        def fake_deliver(url, secret, delivery_id, payload):
            seen.update({"url": url, "payload": payload})
            return True, 200, None

        monkeypatch.setattr(webhooks, "check_url", lambda u: u.strip())
        monkeypatch.setattr(webhooks, "_deliver", fake_deliver)

        made = client.post("/admin/webhooks", headers=admin,
                           json={"url": "https://example.com/h"}).json()
        out = client.post(f"/admin/webhooks/{made['id']}/test", headers=admin).json()

        assert out["delivered"] is True
        assert out["status"] == 200
        assert seen["payload"]["test"] is True, \
            "a test that is not marked as one will be acted on by somebody's handler"

    def test_a_failing_endpoint_is_reported_not_hidden(self, db, tenant, client, admin, monkeypatch):
        from korely_agent.services import webhooks

        monkeypatch.setattr(webhooks, "check_url", lambda u: u.strip())
        monkeypatch.setattr(webhooks, "_deliver",
                            lambda *a, **k: (False, 502, "bad gateway"))

        made = client.post("/admin/webhooks", headers=admin,
                           json={"url": "https://example.com/h"}).json()
        out = client.post(f"/admin/webhooks/{made['id']}/test", headers=admin).json()
        assert out["delivered"] is False
        assert out["error"] == "bad gateway"

    def test_request_activity_counts_refusals_too(self, db, tenant, client, admin):
        """A dashboard that only shows successes cannot show the afternoon
        somebody's key was wrong."""
        from korely_agent.services import audit

        audit.record(db, tenant.id, audit.READ, result="ok")
        audit.record(db, tenant.id, audit.READ, result="error", meta={"code": "forbidden"})

        out = client.get("/admin/requests/summary", headers=admin).json()
        assert out["total"] >= 2
        assert out["refused"] >= 1
        # The shape the screen reads: reads and writes apart, and the
        # endpoints they went to.
        assert out["reads"] + out["writes"] == out["total"]
        assert isinstance(out["by_endpoint"], list)

    def test_the_daily_series_has_no_holes(self, db, tenant, client, admin):
        """A chart with days missing reads as an outage."""
        out = client.get("/admin/requests/daily?days=7", headers=admin).json()
        # `buckets`, the name the chart reads. It also answered to `series`
        # for a few hours: two names for one list is how a field comes to
        # disagree with itself.
        assert "series" not in out
        assert len(out["buckets"]) == 7
        assert all("writes" in d and "reads" in d for d in out["buckets"])

    def test_categories_are_predicate_families(self, db, tenant, client, admin):
        m1, m2 = uuid.uuid4(), uuid.uuid4()
        from korely_agent.db.models import AgentMemory

        db.add_all([
            AgentMemory(id=m1, tenant_id=tenant.id, end_user_id="u1", content="a",
                        project_id=seeded_project_id(db, tenant.id)),
            AgentMemory(id=m2, tenant_id=tenant.id, end_user_id="u1", content="b",
                        project_id=seeded_project_id(db, tenant.id)),
        ])
        db.commit()
        # Distinct objects, because the same triple twice is no longer a state
        # this product can be in: one open fact per triple is enforced by the
        # database now. The service always collapsed these two anyway, so the
        # fixture was building something a real install never produces. What the
        # test is about is unchanged: two memories in the lives_in family, one
        # in works_at.
        # The family is the one stored on the fact (memory_core 3.3.0: no
        # static map of English predicates decides it any more).
        for mid, pred, obj, fam in ((m1, "lives_in", "Rome", "places"), (m2, "lives_in", "Milan", "places"),
                                    (m1, "works_at", "x", "work")):
            db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                             subject_label="Luca", predicate_raw=pred,
                             predicate_canonical=pred, predicate_family=fam, object_label=obj,
                             source_memory_id=mid))
        db.commit()

        cats = client.get("/admin/memories/categories", headers=admin).json()
        counts = {c["category"]: c["memories"] for c in cats}
        # Two memories produced a "lives_in" fact, one produced "works_at".
        # Whatever the families are called, the counts are 2 and 1.
        assert sorted(counts.values()) == [1, 2], counts

    def test_a_forgotten_fact_leaves_its_category(self, db, tenant, client, admin):
        from korely_agent.db.models import AgentMemory

        mid = uuid.uuid4()
        db.add(AgentMemory(id=mid, tenant_id=tenant.id, end_user_id="u1", content="a"))
        db.commit()
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                      subject_label="Luca", predicate_raw="lives in",
                      predicate_canonical="lives_in", object_label="Rome",
                      source_memory_id=mid)
        db.add(f)
        db.commit()

        assert client.get("/admin/memories/categories", headers=admin).json() != []
        client.post(f"/admin/facts/{public(f)}/forget", headers=admin)
        assert client.get("/admin/memories/categories", headers=admin).json() == [], \
            "a category built on facts that are no longer true"


def test_the_administrator_erases_an_end_user_in_every_project(client, db, tenant):
    """DELETE /v1/users/{id}/memories erases in the key's project only; the
    account had no way to erase the same person everywhere (audit of 17 Oct
    2026)."""
    import uuid as _uuid

    from korely_agent.db.models import AgentMemory
    from korely_agent.services import keys as key_service
    from korely_agent.services import projects

    staging = projects.create_project(db, tenant_id=tenant.id, name="staging")
    default = projects.get_or_create_default_project(db, tenant.id)
    for pid in (default.id, staging.id):
        db.add(AgentMemory(id=_uuid.uuid4(), tenant_id=tenant.id, project_id=pid,
                           end_user_id="maria", content="Maria likes tea.",
                           graph_sync_status="done"))
    db.commit()
    _row, plain = key_service.create_api_key(
        db, tenant_id=tenant.id, name="adm", scopes=["memories:read", "memories:write", "admin"])
    r = client.delete("/admin/users/maria/memories", headers={"Authorization": f"Bearer {plain}"})
    assert r.status_code == 200, r.text
    assert r.json()["memories_deleted"] == 2 and r.json()["erasure"] == "permanent"
    db.expire_all()
    assert db.query(AgentMemory).filter(AgentMemory.end_user_id == "maria").count() == 0
