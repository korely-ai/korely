"""The model's usage project by project (1.0.41, 26 Nov 2026).

Until 1.0.40 a cost row (`agent_llm_usage`) named the account, the agent and
the memory, never the project, so the Model usage page and the report could
only say "the whole account". Now each row names the project the call worked
for, the report has a `by_project` grouping and a CSV export, and both add
up to the account's totals: the calls of no project are an entry of their
own, never left out.
"""
import csv
import io
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.core.llm import LLMResult
from korely_agent.db.models import AgentLlmUsage, AgentMemory
from korely_agent.services import keys as key_service
from korely_agent.services import llm_usage, projects, usage_report

CARD = ('{"currency":"EUR","models":{'
        '"m-priced":{"input_per_million":1.0,"output_per_million":10.0}}}')


@pytest.fixture(autouse=True)
def own_session(db, monkeypatch):
    """`flush` opens its own session; the test's one sees what it writes."""
    monkeypatch.setattr("korely_agent.db.get_own_sessionmaker", lambda: (lambda: db))
    yield


def _project(db, tenant, name):
    projects.get_or_create_default_project(db, tenant.id)
    p = projects.create_project(db, tenant_id=tenant.id, name=name)
    db.commit()
    return p


def _call(tenant_id, project_id, *, model="m-priced", tin=1000, tout=100, agent="bot"):
    """One call of `tenant_id`'s. Its row is written through the test's own
    session (`own_session`), which `flush` closes: callers hold ids, not rows."""
    with llm_usage.attributed(tenant_id, agent_id=agent, project_id=project_id):
        llm_usage.record("mistral", model, tin, tout)


def _rows(db, tenant_id):
    return db.query(AgentLlmUsage).filter(AgentLlmUsage.tenant_id == tenant_id).all()


class TestTheRowNamesItsProject:
    def test_a_call_in_a_project_is_filed_there(self, db, tenant):
        tid = tenant.id
        p = _project(db, tenant, "Client A")
        _call(tid, p.id)
        (r,) = _rows(db, tid)
        assert r.project_id == p.id

    def test_a_scope_of_no_single_project_files_none(self, db, tenant):
        tid = tenant.id
        from korely_agent.services.sentinels import EVERY_PROJECT

        _call(tid, EVERY_PROJECT)
        _call(tid, None)
        assert [r.project_id for r in _rows(db, tid)] == [None, None]

    def test_a_project_deleted_during_its_calls_keeps_the_cost_without_it(self, db, tenant):
        """The project's row went while the call ran: the foreign key refuses
        the row, and the paid call is kept for the account, as for a memory
        gone during its calls."""
        tid = tenant.id
        _call(tid, uuid.uuid4())
        (r,) = _rows(db, tid)
        assert r.project_id is None and r.prompt_tokens == 1000

    def test_the_drain_files_a_memory_s_calls_in_its_project(self, db, tenant, monkeypatch):
        tid = tenant.id
        p = _project(db, tenant, "Client B")
        seen = []

        class Fake:
            name = "fake"

            def complete(self, messages, **kw):
                scope = llm_usage._active.get()
                seen.append(scope.project_id if scope else "none")
                llm_usage.record("mistral", "m-priced", 10, 1)
                return LLMResult(text='{"facts": []}')

        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: Fake())
        from korely_agent.services.facts.service import extract_and_persist_facts

        m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1", agent_id="bot",
                        project_id=p.id, content="Maria is on the Starter plan.")
        db.add(m)
        db.commit()
        extract_and_persist_facts(db, tenant.id, memory=m)
        assert seen and set(seen) == {p.id}
        assert {r.project_id for r in _rows(db, tid)} == {p.id}

    def test_a_correction_read_across_projects_is_filed_in_the_fact_s_project(self, db, tenant):
        """The dashboard corrects with every project in its scope; the
        corrected fact is written in the old one's project, and so is its
        cost (`llm_usage.in_project`)."""
        tid = tenant.id
        p = _project(db, tenant, "Client C")
        with llm_usage.attributed(tenant.id, project_id=None):
            llm_usage.in_project(p.id)
            llm_usage.record("mistral", "m-priced", 5, 1)
        (r,) = _rows(db, tid)
        assert r.project_id == p.id

    def test_the_correction_names_the_fact_s_project_in_the_service(self):
        """`correct_fact` tells the scope the project of the fact it loaded."""
        import inspect

        from korely_agent.services.facts import service

        src = inspect.getsource(service.correct_fact)
        assert "llm_usage.in_project(old.project_id)" in src


class TestTheReportAddsUp:
    def test_by_project_adds_up_to_the_totals_with_no_project_its_own_entry(self, db, tenant):
        tid = tenant.id
        a = _project(db, tenant, "Client A")
        b = _project(db, tenant, "Client B")
        _call(tid, a.id, tin=1000, tout=100)
        _call(tid, a.id, model="m-unpriced", tin=500, tout=50)
        _call(tid, b.id, tin=2000, tout=300, agent="other")
        _call(tid, None, tin=70, tout=7)
        r = usage_report.report(db, tenant.id, rates_raw=CARD)
        by = {e["project_id"]: e for e in r["by_project"]}
        assert set(by) == {str(a.id), str(b.id), None}
        assert by[str(a.id)]["name"] == "Client A" and by[None]["name"] is None
        for field in ("calls", "prompt_tokens", "completion_tokens", "cached_tokens"):
            assert sum(e[field] for e in r["by_project"]) == r["totals"][field], field
        assert by[str(a.id)]["calls"] == 2
        assert sum(e["cost"] or 0 for e in r["by_project"]) == pytest.approx(r["totals"]["cost"])
        # a project's cost is priced on its own models only: the unpriced one adds nothing
        assert by[str(a.id)]["cost"] == pytest.approx((1000 * 1.0 + 100 * 10.0) / 1e6)

    def test_another_account_s_calls_are_never_in_the_report(self, db, tenant):
        tid = tenant.id
        from korely_agent.db.models import Tenant

        other = Tenant(id=uuid.uuid4(), name="Other")
        db.add(other)
        db.commit()
        p = _project(db, tenant, "Mine")
        _call(tid, p.id)
        _call(other.id, None)
        r = usage_report.report(db, tenant.id, rates_raw=CARD)
        assert [e["project_id"] for e in r["by_project"]] == [str(p.id)]
        assert r["totals"]["calls"] == 1

    def test_a_deleted_project_hands_its_calls_to_the_default_one(self, db, tenant):
        tid = tenant.id
        pid = _project(db, tenant, "Going").id
        default_id = projects.get_default_project(db, tid).id
        _call(tid, pid, tin=10, tout=1)
        out = projects.delete_project(db, tid, projects.get_project(db, tid, pid))
        db.commit()
        assert out["costs"] == 1
        assert {r.project_id for r in _rows(db, tid)} == {default_id}
        r = usage_report.report(db, tid, rates_raw=CARD)
        assert [e["project_id"] for e in r["by_project"]] == [str(default_id)]


class TestTheExport:
    @pytest.fixture
    def client(self, db, monkeypatch):
        monkeypatch.setenv("RUN_WORKER", "false")
        monkeypatch.setenv("LLM_RATES", CARD)
        from korely_agent.config import get_settings
        from korely_agent.db import get_db
        from korely_agent.main import create_app

        get_settings.cache_clear()
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    def _admin(self, db, tenant):
        _r, admin = key_service.create_api_key(
            db, tenant_id=tenant.id, name="operator",
            scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
        return {"Authorization": f"Bearer {admin}"}

    def test_the_csv_has_a_row_per_project_and_model_adding_up_to_the_report(self, db, tenant, client):
        tid = tenant.id
        a = _project(db, tenant, "=HYPERLINK(\"x\")")
        _call(tid, a.id, tin=1000, tout=100)
        _call(tid, a.id, model="m-unpriced", tin=500, tout=50)
        _call(tid, None, tin=70, tout=7)
        headers = self._admin(db, tenant)
        got = client.get("/admin/usage/export", headers=headers)
        assert got.status_code == 200
        assert got.headers["content-type"].startswith("text/csv")
        assert "attachment" in got.headers["content-disposition"]
        table = list(csv.DictReader(io.StringIO(got.text)))
        assert list(table[0]) == list(usage_report.EXPORT_COLUMNS)
        assert len(table) == 3
        # a name a spreadsheet would run is written after a quote
        assert {r["project"] for r in table if r["project_id"]} == {"'=HYPERLINK(\"x\")"}
        report = client.get("/admin/usage", headers=headers).json()
        assert sum(int(r["calls"]) for r in table) == report["totals"]["calls"]
        assert sum(int(r["prompt_tokens"]) for r in table) == report["totals"]["prompt_tokens"]
        assert sum(float(r["cost"] or 0) for r in table) == pytest.approx(report["totals"]["cost"])
        assert {r["currency"] for r in table} == {"EUR"}

    def test_another_account_s_rows_and_project_names_never_reach_the_export(self, db, tenant, client):
        """Two accounts: the export and the report of one hold its rows only,
        and never another account's project name, even on a row of its own
        that names that project (not made by any door; asked anyway)."""
        from korely_agent.db.models import Tenant

        tid = tenant.id
        other = Tenant(id=uuid.uuid4(), name="Other")
        db.add(other)
        db.commit()
        oid = other.id
        mine = _project(db, tenant, "Mine").id
        theirs = _project(db, other, "SECRET-THEIRS").id
        _call(tid, mine, tin=1000, tout=100)
        _call(oid, theirs, tin=5000, tout=500)
        _call(oid, None, tin=7, tout=1)
        _call(tid, theirs, tin=10, tout=1)
        headers = self._admin(db, tenant)
        got = client.get("/admin/usage/export", headers=headers)
        report = client.get("/admin/usage", headers=headers).json()
        assert "SECRET-THEIRS" not in got.text and "SECRET-THEIRS" not in str(report)
        table = list(csv.DictReader(io.StringIO(got.text)))
        assert sum(int(r["calls"]) for r in table) == report["totals"]["calls"] == 2
        assert sum(int(r["prompt_tokens"]) for r in table) == 1010
        assert {r["project"] for r in table} == {"Mine", ""}

    @pytest.mark.parametrize("start", ["=", "+", "-", "@"])
    def test_a_name_a_spreadsheet_would_run_is_written_after_a_quote(self, db, tenant, client, start):
        tid = tenant.id
        pid = _project(db, tenant, f"{start}1+1 Client").id
        _call(tid, pid)
        got = client.get("/admin/usage/export", headers=self._admin(db, tenant))
        (row,) = list(csv.DictReader(io.StringIO(got.text)))
        assert row["project"] == f"'{start}1+1 Client"

    def test_the_export_is_operator_only(self, db, tenant, client):
        _r, agent = key_service.create_api_key(db, tenant_id=tenant.id, name="agent")
        assert client.get("/admin/usage/export",
                          headers={"Authorization": f"Bearer {agent}"}).status_code == 403


def test_the_migration_adds_the_column_the_model_has():
    """The chain's head adds `project_id` to the cost rows, with the index the
    report reads."""
    import pathlib

    text = (pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions"
            / "a3d7f9c1e5b2_a_model_call_names_its_project.py").read_text()
    assert 'down_revision = "c4e9a7d2b5f8"' in text
    assert "idx_llm_usage_tenant_project_ts" in text
    assert "project_id" in AgentLlmUsage.__table__.columns
