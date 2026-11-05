"""A writer refused for payment is told, and /health says so (15 Oct 2026).

The Gemini prepaid credit ran out: the drain rested the region, the only
trace was a log line, and /health kept saying "working". Now the HTTP status
of a failed call is kept as a field (`core.llm._status_of`, never words of
the message), the first refusal of a rest is remembered with its time
(`writers_refused`, shown on /health), and one for payment (402) is told once
per rest through the `writer_refused` hook, which the hosted service turns
into an email to its operator.
"""
import urllib.error
import uuid

import pytest

from korely_agent.core import extensions, llm
from korely_agent.services.facts import drain as facts_drain
from korely_agent.services.facts import service as facts_service


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(facts_drain, "_pauses", {})
    told = []
    extensions.register("writer_refused", lambda region, status, detail: told.append((region, status)))
    yield told
    extensions.reset()


def test_the_status_is_a_field_of_the_answer():
    class GenaiLike(Exception):
        code = 402

    http = urllib.error.HTTPError("https://x", 402, "Payment Required", {}, None)
    assert llm._status_of(GenaiLike("whatever it says")) == 402
    assert llm._status_of(http) == 402
    assert llm._status_of(TimeoutError("timed out")) is None
    assert llm._status_of(type("E", (Exception,), {"code": "402"})()) is None


def test_a_failed_call_keeps_its_status_for_the_drain():
    llm._record(False, "APIError: 402 RESOURCE_EXHAUSTED", status=402)
    assert llm.last_status_here() == 402
    llm._record(True)
    assert llm.last_status_here() is None


def test_payment_is_told_once_per_rest(fresh):
    facts_drain._provider_refused("global", 402)
    facts_drain._provider_refused("global", 402)  # still resting
    assert fresh == [("global", 402)]
    w = facts_service.writers_refused()["global"]
    assert w["state"] == "refused" and w["payment_required"] and w["since"]
    facts_drain._provider_answered("global")
    assert facts_service.writers_refused() == {}
    facts_drain._pauses["global"]["until"] = 0.0
    facts_drain._provider_refused("global", 402)
    assert fresh == [("global", 402), ("global", 402)]


def test_a_rate_limit_rests_and_is_not_told(fresh):
    facts_drain._provider_refused("eu", 429)
    assert fresh == []
    assert facts_service.writers_refused()["eu"]["payment_required"] is False


def test_an_alert_that_fails_never_stops_the_drain(monkeypatch):
    def boom(*a):
        raise RuntimeError("mailer down")

    extensions.register("writer_refused", boom)
    facts_drain._provider_refused("global", 402)
    assert facts_drain._drain_paused("global") > 0


@pytest.mark.cloud
def test_the_hosted_alert_names_the_region_and_the_status(monkeypatch):
    from korely_agent.hosted import alerts

    sent = []
    monkeypatch.setattr(alerts, "tell_the_founder", lambda subject, body: sent.append((subject, body)))
    alerts.writer_refused("global", 402, "Your prepayment credits are depleted.")
    subject, body = sent[0]
    assert "global" in subject and "HTTP 402" in body


@pytest.mark.cloud
def test_the_hosted_alert_never_carries_the_providers_words(monkeypatch):
    """Audit of 17 Oct 2026: the email quoted the provider's message,
    text nobody chose, which may repeat the request."""
    from korely_agent.hosted import alerts

    sent = []
    monkeypatch.setattr(alerts, "tell_the_founder", lambda subject, body: sent.append((subject, body)))
    words = "credits depleted for the request 'my sister Anna lives in Turin'"
    alerts.writer_refused("eu", 402, words)
    subject, body = sent[0]
    assert "Anna" not in subject + body and "depleted" not in subject + body


def test_health_shows_the_refused_region_and_degrades(db, monkeypatch):
    from fastapi.testclient import TestClient

    from korely_agent import config
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    config.get_settings.cache_clear()
    try:
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as c:
            facts_drain._provider_refused("global", 402)
            body = c.get("/health").json()
        assert body["writers"]["global"]["payment_required"] is True
        assert body["status"] == "degraded"
    finally:
        config.get_settings.cache_clear()


def test_the_drain_tells_a_payment_refusal(db, tenant, monkeypatch, fresh):
    from korely_agent.db.models import AgentMemory

    class Broke:
        name = "gemini"

        def complete(self, *a, **k):
            llm._record(False, "APIError: 402 RESOURCE_EXHAUSTED", status=402)
            raise RuntimeError("402 RESOURCE_EXHAUSTED")

    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: Broke())
    db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                       content="hello", graph_sync_status="pending"))
    db.commit()
    facts_service.process_pending_memories(db, limit=5)
    assert [s for _r, s in fresh] == [402]


def test_the_hook_is_told_the_kind_never_the_providers_words(db, tenant, monkeypatch):
    """The hook was handed `failure[:500]`, the provider's own words, which
    can quote the request (audit of 17 Oct 2026)."""
    from korely_agent.db.models import AgentMemory

    details = []
    extensions.register("writer_refused", lambda region, status, detail: details.append(detail))

    class Broke:
        name = "gemini"

        def complete(self, *a, **k):
            llm._record(False, "APIError: 402 credit of zz-marker-maria@example.test",
                        status=402, kind=llm.REFUSED)
            raise RuntimeError("402 credit of zz-marker-maria@example.test")

    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: Broke())
    db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                       content="hello", graph_sync_status="pending"))
    db.commit()
    facts_service.process_pending_memories(db, limit=5)
    assert details == [llm.REFUSED]
