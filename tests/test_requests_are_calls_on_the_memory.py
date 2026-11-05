"""The Requests page counts the calls on the memory, not the administration.

The audit of 6 October brought the administration to the trail (a key made or
moved, a setting, a login: services/audit.py). The Requests views counted
every verb that was not a write as a read, so on the day it landed a login
showed on the Home page as a retrieval event, under "search · context ·
facts", and in the list of "every API call your agents make". Found clicking
through a fresh install's dashboard, 2026-10-06: two retrieval events and two
write events before anybody had read or written a memory.
"""
import pytest
from fastapi.testclient import TestClient

from korely_agent.services import audit, usage_report
from korely_agent.services import keys as key_service


def test_every_verb_is_in_exactly_one_set():
    """A new verb makes somebody decide where it goes."""
    sets = (usage_report._READ_ACTIONS, usage_report._WRITE_ACTIONS, usage_report.ADMINISTRATION)
    assert set().union(*sets) == audit._ALLOWED
    assert sum(len(s) for s in sets) == len(audit._ALLOWED)


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
def a_day_of_work(db, tenant, admin):
    """One read and one write by an agent; around them, the administration."""
    audit.record(db, tenant.id, audit.READ, actor="rest", meta={"endpoint": "context:get"})
    audit.record(db, tenant.id, audit.WRITE, actor="rest", meta={"endpoint": "memories:add"})
    for verb in sorted(usage_report.ADMINISTRATION):
        audit.record(db, tenant.id, verb, actor="dashboard")


class TestTheRequestsViews:
    def test_the_summary_counts_one_read_and_one_write(self, client, admin, a_day_of_work):
        out = client.get("/admin/requests/summary", headers=admin).json()
        assert (out["reads"], out["writes"], out["total"]) == (1, 1, 2)
        assert {e["endpoint"] for e in out["by_endpoint"]} == {"context:get", "memories:add"}

    def test_the_daily_series_counts_them_too(self, client, admin, a_day_of_work):
        out = client.get("/admin/requests/daily?days=7", headers=admin).json()
        assert (out["total_reads"], out["total_writes"]) == (1, 1)

    def test_the_list_shows_the_two_calls(self, client, admin, a_day_of_work):
        out = client.get("/admin/requests", headers=admin).json()
        assert sorted(r["action"] for r in out["requests"]) == [audit.READ, audit.WRITE]

    def test_the_administration_stays_in_the_audit_log(self, client, admin, a_day_of_work):
        out = client.get("/admin/audit?limit=500", headers=admin).json()
        assert usage_report.ADMINISTRATION <= {e["action"] for e in out["events"]}
