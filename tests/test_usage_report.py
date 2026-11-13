"""The consumption report: the page the hosted product cannot have.

There the model bill is the vendor's margin and stays hidden. Here it arrives
at the operator's own account, so the number has to be right and, where it
cannot be right, it has to say so rather than read as zero. Those two failures
are what most of this file is about: a missing rate card must not look like a
free month, and a model nobody priced must not be priced at nothing.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentLlmUsage, Tenant
from korely_agent.services import keys as key_service
from korely_agent.services import usage_report


CARD = ('{"currency":"EUR","models":{'
        '"gemini-2.5-flash":{"input_per_million":1.0,"output_per_million":10.0}}}')


def used(db, tenant, *, stage="extraction", model="gemini-2.5-flash",
         tin=0, tout=0, agent=None, ago_days=0, cached=0):
    db.add(AgentLlmUsage(
        id=uuid.uuid4(), tenant_id=tenant.id, stage=stage, provider="gemini",
        model=model, prompt_tokens=tin, completion_tokens=tout, agent_id=agent,
        cached_tokens=cached, ts=datetime.now(timezone.utc) - timedelta(days=ago_days)))
    db.commit()


class TestTheCache:
    """2026-10-07: the prompt tokens a provider served from its cache, part of
    the tokens in, counted per model, stage and agent, and priced with them."""

    def test_counted_where_the_tokens_are(self, db, tenant):
        used(db, tenant, tin=4000, tout=300, cached=3000, agent="a")
        used(db, tenant, stage="contradiction", tin=1300, tout=90, cached=0, agent="a")
        r = usage_report.report(db, tenant.id, rates_raw=CARD)
        assert r["totals"]["prompt_tokens"] == 5300 and r["totals"]["cached_tokens"] == 3000
        assert {s["stage"]: s["cached_tokens"] for s in r["by_stage"]} == {"extraction": 3000,
                                                                           "contradiction": 0}
        assert r["by_model"][0]["cached_tokens"] == 3000 and r["by_agent"][0]["cached_tokens"] == 3000
        assert r["totals"]["cost"] == pytest.approx((5300 * 1.0 + 390 * 10.0) / 1e6)


class TestTheArithmetic:
    def test_cost_is_per_million_tokens(self, db, tenant):
        used(db, tenant, tin=1_000_000, tout=100_000)
        r = usage_report.report(db, tenant.id, rates_raw=CARD)
        # 1.0 for the input million, 10.0 per output million on a tenth of one
        assert r["totals"]["cost"] == pytest.approx(1.0 + 1.0)
        assert r["currency"] == "EUR"

    def test_grouped_three_ways(self, db, tenant):
        used(db, tenant, stage="extraction", tout=100, agent="a")
        used(db, tenant, stage="contradiction", tout=300, agent="b")
        used(db, tenant, stage="extraction", tout=50, agent="b")

        r = usage_report.report(db, tenant.id, rates_raw=CARD)
        stages = {s["stage"]: s["completion_tokens"] for s in r["by_stage"]}
        agents = {a["agent_id"]: a["completion_tokens"] for a in r["by_agent"]}
        assert stages == {"extraction": 150, "contradiction": 300}
        assert agents == {"a": 100, "b": 350}
        assert r["totals"]["completion_tokens"] == 450
        assert r["totals"]["calls"] == 3

    def test_the_most_expensive_comes_first(self, db, tenant):
        used(db, tenant, stage="embedding", tout=1)
        used(db, tenant, stage="extraction", tout=900)
        r = usage_report.report(db, tenant.id)
        assert r["by_stage"][0]["stage"] == "extraction"

    def test_the_window_holds(self, db, tenant):
        used(db, tenant, tout=10, ago_days=5)
        used(db, tenant, tout=20, ago_days=45)
        assert usage_report.report(db, tenant.id, days=30)["totals"]["completion_tokens"] == 10
        assert usage_report.report(db, tenant.id, days=90)["totals"]["completion_tokens"] == 30


class TestWhenItCannotPriceSomething:
    def test_no_rate_card_means_null_not_zero(self, db, tenant):
        """Zero reads as "nothing was spent". Null reads as "nobody said what a
        token costs here", which is what actually happened."""
        used(db, tenant, tin=1000, tout=1000)
        r = usage_report.report(db, tenant.id, rates_raw=None)
        assert r["rates_configured"] is False
        assert r["totals"]["cost"] is None
        assert r["totals"]["completion_tokens"] == 1000, "the counts still come out"
        assert all(s["cost"] is None for s in r["by_stage"])

    def test_a_model_with_no_rate_is_named_not_priced_at_zero(self, db, tenant):
        used(db, tenant, model="gemini-2.5-flash", tin=1_000_000, tout=0)
        used(db, tenant, model="some-new-model", tin=9_000_000, tout=0)

        r = usage_report.report(db, tenant.id, rates_raw=CARD)
        assert r["models_without_a_rate"] == ["some-new-model"]
        # The priced one is counted, the unpriced one is not silently free.
        assert r["totals"]["cost"] == pytest.approx(1.0)
        unpriced = [m for m in r["by_model"] if m["model"] == "some-new-model"][0]
        assert unpriced["cost"] is None
        assert unpriced["prompt_tokens"] == 9_000_000

    def test_a_stage_made_only_of_unpriced_models_costs_null(self, db, tenant):
        used(db, tenant, stage="embedding", model="some-embedder", tin=500)
        r = usage_report.report(db, tenant.id, rates_raw=CARD)
        emb = [s for s in r["by_stage"] if s["stage"] == "embedding"][0]
        assert emb["cost"] is None
        assert emb["calls"] == 1

    def test_a_broken_rate_card_is_loud_and_blank(self, db, tenant, caplog):
        import logging

        used(db, tenant, tout=1000)
        with caplog.at_level(logging.ERROR):
            r = usage_report.report(db, tenant.id, rates_raw="{not json")
        assert r["rates_configured"] is False
        assert r["totals"]["cost"] is None
        assert "rate card" in caplog.text, "it failed silently"

    def test_a_card_without_models_is_also_refused(self, db, tenant):
        assert usage_report.parse_rates('{"currency":"EUR"}') is None


class TestAttribution:
    def test_calls_with_no_agent_are_counted_and_named(self, db, tenant):
        """agent_id is optional on a write, and the per-agent bill is the whole
        point. An empty breakdown with no explanation reads as no spend."""
        used(db, tenant, tout=10, agent="support-bot")
        used(db, tenant, tout=20, agent=None)
        used(db, tenant, tout=30, agent=None)

        r = usage_report.report(db, tenant.id)
        assert r["calls_without_an_agent_id"] == 2
        assert any(a["agent_id"] is None for a in r["by_agent"])


class TestIsolation:
    def test_another_account_is_not_in_the_bill(self, db, tenant):
        other = Tenant(id=uuid.uuid4(), name="Somebody Else")
        db.add(other)
        db.commit()

        used(db, tenant, tout=100, agent="mine")
        used(db, other, tout=999_999, agent="theirs")

        r = usage_report.report(db, tenant.id)
        assert r["totals"]["completion_tokens"] == 100
        assert [a["agent_id"] for a in r["by_agent"]] == ["mine"]


class TestTheEndpoint:
    @pytest.fixture
    def client(self, db, monkeypatch):
        monkeypatch.setenv("RUN_WORKER", "false")
        from korely_agent.db import get_db
        from korely_agent.main import create_app

        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()

    def test_it_is_operator_only(self, db, tenant, client):
        _r, agent = key_service.create_api_key(db, tenant_id=tenant.id, name="agent")
        _r2, admin = key_service.create_api_key(
            db, tenant_id=tenant.id, name="operator",
            scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])

        assert client.get("/admin/usage",
                          headers={"Authorization": f"Bearer {agent}"}).status_code == 403
        assert client.get("/admin/usage",
                          headers={"Authorization": f"Bearer {admin}"}).status_code == 200

    def test_an_empty_month_answers_with_zeroes_not_an_error(self, db, tenant, client):
        _r, admin = key_service.create_api_key(
            db, tenant_id=tenant.id, name="operator",
            scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
        body = client.get("/admin/usage",
                          headers={"Authorization": f"Bearer {admin}"}).json()
        assert body["totals"] == {"calls": 0, "prompt_tokens": 0,
                                  "completion_tokens": 0, "cached_tokens": 0, "cost": None}
        assert body["by_agent"] == []
