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


@pytest.fixture
def the_screen_looked(db, tenant, a_day_of_work):
    """An operator signed in to the dashboard opens pages: each read of the
    memory is filed in the trail by the screen, as every read of personal
    data is; one fact corrected there is a change to the memory."""
    for endpoint in ("admin:facts:list", "admin:memories:list", "admin:facts:list"):
        audit.record(db, tenant.id, audit.READ, actor="dashboard", meta={"endpoint": endpoint})
    audit.record(db, tenant.id, audit.FACT_WRITE, actor="dashboard", meta={"endpoint": "admin:facts:correct"})


class TestTheScreensOwnReadsAreNotRequests:
    """QA of 14 Nov 2026: an account nobody's agent had called showed 12
    reads on the Requests page (the screen's own, as it was browsed), and
    the Home page, read a few clicks before, said 0. The screen's reads stay
    in the Audit log; every view of the requests counts the same calls."""

    def test_the_summary_the_timeline_and_the_list_count_the_same_calls(
            self, client, admin, the_screen_looked):
        summary = client.get("/admin/requests/summary", headers=admin).json()
        daily = client.get("/admin/requests/daily", headers=admin).json()
        listed = client.get("/admin/requests", headers=admin).json()["requests"]
        assert (summary["reads"], summary["writes"]) == (1, 2), "the agent's read; its write and the correction"
        assert (daily["total_reads"], daily["total_writes"]) == (summary["reads"], summary["writes"])
        assert len(listed) == summary["total"] == 3
        assert "admin:facts:list" not in {e["endpoint"] for e in summary["by_endpoint"]}

    def test_the_screens_reads_stay_in_the_audit_log(self, client, admin, the_screen_looked):
        events = client.get("/admin/audit?limit=500", headers=admin).json()["events"]
        assert sum(1 for e in events if e["action"] == audit.READ and e["actor"] == "dashboard") == 3

    def test_an_admin_keys_read_is_still_a_request(self, db, tenant, client, admin):
        """Only the screen is left out: a script with an admin key reading
        the memory over /admin is a caller like any other."""
        audit.record(db, tenant.id, audit.READ, actor="rest", meta={"endpoint": "admin:facts:list"})
        assert client.get("/admin/requests/summary", headers=admin).json()["reads"] == 1


def test_the_total_and_the_chart_begin_at_the_same_moment(db, tenant, client, admin):
    """The summary began `days` days back to the second and the timeline at
    midnight UTC, so a call between the two was in the total and not in the
    chart beside it."""
    from datetime import datetime, timedelta, timezone

    from korely_agent.db import AuditEvent

    start = usage_report._window_start(7)
    assert start == (datetime.now(timezone.utc) - timedelta(days=6)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    # A call between the two old beginnings: 7 days back to the second, and
    # midnight of the 7th day, today counted (the gap is never empty).
    between = start - (start - (datetime.now(timezone.utc) - timedelta(days=7))) / 2
    mine = db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id)
    audit.record(db, tenant.id, audit.READ, actor="rest", meta={"endpoint": "context:get"})
    mine.update({AuditEvent.ts: between})
    db.commit()
    audit.record(db, tenant.id, audit.READ, actor="rest", meta={"endpoint": "context:get"})
    mine.filter(AuditEvent.ts >= start).update({AuditEvent.ts: start + timedelta(minutes=1)})
    db.commit()
    summary = client.get("/admin/requests/summary?days=7", headers=admin).json()
    daily = client.get("/admin/requests/daily?days=7", headers=admin).json()
    assert summary["reads"] == daily["total_reads"] == 1
