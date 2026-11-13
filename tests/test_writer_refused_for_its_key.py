"""A writer refused for its key is told, and /health degrades (10 Nov 2026).

On 4 Nov 2026 the Scaleway key of the Cloud's European writer was revoked:
the writer answered 403 from 21:08 UTC, the European memories waited in the
queue as they must (no region writes another's), and for about eleven hours
nothing said so but a log line and `refused` on /health, whose `status`
stayed `ok`. Only a refusal for payment (402) was told, through the
`writer_refused` hook the hosted service turns into an email, since the
Gemini credit of 15 Oct 2026. A refusal of the key (401, 403: RFC 9110
15.5.2 and 15.5.4) waits for a person just the same, and is now told the
same way, once per rest; a rate or a server (429, 5xx) ends by itself and
is not.
"""
import uuid

import pytest

from korely_agent.core import extensions, llm
from korely_agent.services.facts import drain as facts_drain
from korely_agent.services.facts import service as facts_service


@pytest.fixture(autouse=True)
def told(monkeypatch):
    monkeypatch.setattr(facts_drain, "_pauses", {})
    heard = []
    extensions.register("writer_refused", lambda region, status, detail: heard.append((region, status)))
    yield heard
    extensions.reset()


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_is_told_once_per_rest(told, status):
    facts_drain._provider_refused("eu", status, llm.REFUSED)
    facts_drain._provider_refused("eu", status, llm.REFUSED)  # still resting
    assert told == [("eu", status)]
    facts_drain._pauses["eu"]["until"] = 0.0
    facts_drain._provider_refused("eu", status, llm.REFUSED)  # the same rest, longer
    assert told == [("eu", status)]
    facts_drain._provider_answered("eu")
    facts_drain._pauses["eu"]["until"] = 0.0
    facts_drain._provider_refused("eu", status, llm.REFUSED)
    assert told == [("eu", status), ("eu", status)]


@pytest.mark.parametrize("before", [429, 503, None])
@pytest.mark.parametrize("status", [401, 403])
def test_a_key_refused_after_a_rate_in_the_same_rest_is_told(told, before, status):
    """The sceptic's probe R1 (10 Nov 2026): a rest begun by a refusal that
    ends by itself, then a refused key at the end of its pause, the order of
    a 429 or a 503 just before a key is revoked. The rest's first refusal
    decided alone, so it was told nothing and /health kept the 429."""
    facts_drain._provider_refused("eu", before, llm.REFUSED)
    assert told == []
    facts_drain._pauses["eu"]["until"] = 0.0
    facts_drain._provider_refused("eu", status, llm.REFUSED)
    assert told == [("eu", status)]
    assert facts_service.writers_refused()["eu"]["status"] == status
    facts_drain._pauses["eu"]["until"] = 0.0
    facts_drain._provider_refused("eu", before, llm.REFUSED)  # a rate again: still the key's rest
    assert told == [("eu", status)]
    assert facts_service.writers_refused()["eu"]["status"] == status


@pytest.mark.parametrize("status", [401, 403])
def test_a_key_refused_during_the_pause_is_told_and_does_not_lengthen_it(told, status):
    """A group still in flight when the region began to rest on a 429 meets
    the revoked key: told at once, and the pause is the one it was."""
    facts_drain._provider_refused("eu", 429, llm.REFUSED)
    until, step = facts_drain._pauses["eu"]["until"], facts_drain._pauses["eu"]["step"]
    facts_drain._provider_refused("eu", status, llm.REFUSED)
    assert told == [("eu", status)]
    assert (facts_drain._pauses["eu"]["until"], facts_drain._pauses["eu"]["step"]) == (until, step)
    assert facts_service.writers_refused()["eu"]["status"] == status
    facts_drain._provider_refused("eu", status, llm.REFUSED)
    assert told == [("eu", status)]


def test_a_rest_a_memory_ended_is_told_at_its_next_refusal(told):
    """A memory written during the pause ends the rest (`_provider_answered`):
    a refused key in what is left of the pause begins nothing, and the next
    refusal begins the next rest and is told."""
    facts_drain._provider_refused("eu", 429, llm.REFUSED)
    facts_drain._provider_answered("eu")
    facts_drain._provider_refused("eu", 403, llm.REFUSED)
    assert told == [] and facts_service.writers_refused() == {}
    facts_drain._pauses["eu"]["until"] = 0.0
    facts_drain._provider_refused("eu", 403, llm.REFUSED)
    assert told == [("eu", 403)]


def test_health_degrades_when_a_rate_limit_turns_into_a_refused_key(db, monkeypatch):
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
            facts_drain._provider_refused("global", 429, llm.REFUSED)
            before = c.get("/health").json()
            facts_drain._pauses["global"]["until"] = 0.0
            facts_drain._provider_refused("global", 403, llm.REFUSED)
            after = c.get("/health").json()
        assert before["status"] == "ok" and after["status"] == "degraded"
        assert after["writers"]["global"]["status"] == 403
    finally:
        config.get_settings.cache_clear()


@pytest.mark.parametrize("status", [None, 408, 429, 500, 502, 503, 504, 529])
def test_a_rate_or_a_server_rests_and_is_not_told(told, status):
    facts_drain._provider_refused("eu", status, llm.REFUSED)
    assert facts_drain._drain_paused("eu") > 0
    assert told == []


@pytest.mark.parametrize("status,waits", [(401, True), (402, True), (403, True), (429, False),
                                          (503, False), (None, False)])
def test_which_refusals_wait_for_a_person(status, waits):
    assert facts_drain.waits_for_a_person(status) is waits


@pytest.mark.parametrize("status", [401, 403])
def test_health_degrades_while_a_key_is_refused(db, monkeypatch, status):
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
            assert c.get("/health").json()["status"] == "ok"
            facts_drain._provider_refused("global", status, llm.REFUSED)
            body = c.get("/health").json()
            facts_drain._provider_answered("global")
            after = c.get("/health").json()
        assert body["writers"]["global"]["state"] == "refused"
        assert body["writers"]["global"]["payment_required"] is False
        assert body["status"] == "degraded"
        assert after["status"] == "ok"
    finally:
        config.get_settings.cache_clear()


def test_health_does_not_degrade_for_a_rate_limit(db, monkeypatch):
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
            facts_drain._provider_refused("global", 429, llm.REFUSED)
            body = c.get("/health").json()
        assert body["writers"]["global"]["state"] == "refused"
        assert body["status"] == "ok"
    finally:
        config.get_settings.cache_clear()


def test_the_drain_tells_a_revoked_key(db, tenant, monkeypatch, told):
    """Through the drain: the only account writing, its memory refused 403
    (the European key of 4 Nov 2026), rests the region and is told once."""
    from korely_agent.db.models import AgentMemory

    class Revoked:
        name = "mistral"

        def complete(self, *a, **k):
            llm._record(False, "HTTPError: 403", status=403, kind=llm.REFUSED)
            raise RuntimeError("403 Forbidden")

    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: Revoked())
    db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                       content="hello", graph_sync_status="pending"))
    db.commit()
    facts_service.process_pending_memories(db, limit=5)
    assert [s for _r, s in told] == [403]
    db.expire_all()
    assert [m.graph_sync_status for m in db.query(AgentMemory).all()] == ["pending"]


def _a_hook_that_raises():
    def boom(region, status, detail):
        raise RuntimeError("mailer down for the request 'my sister Anna lives in Turin'")
    extensions.reset()
    extensions.register("writer_refused", boom)


@pytest.mark.parametrize("status", [401, 403])
def test_a_hook_that_raises_never_stops_the_refusal(status, caplog):
    """The sceptic's R2 (10 Nov 2026): no test had a hook that raises for a
    refused key, and a drain without its `except` kept every test green."""
    import logging

    _a_hook_that_raises()
    with caplog.at_level(logging.WARNING, logger=facts_drain.logger.name):
        facts_drain._provider_refused("eu", status, llm.REFUSED)
    assert facts_drain._drain_paused("eu") > 0
    assert facts_service.writers_refused()["eu"]["status"] == status
    assert "the refusal could not be told" in caplog.text
    assert "Anna" not in caplog.text and "mailer down" not in caplog.text


def test_a_hook_that_raises_never_stops_the_drain(db, tenant, monkeypatch):
    from korely_agent.db.models import AgentMemory

    class Revoked:
        name = "mistral"

        def complete(self, *a, **k):
            llm._record(False, "HTTPError: 403", status=403, kind=llm.REFUSED)
            raise RuntimeError("403 Forbidden")

    _a_hook_that_raises()
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: Revoked())
    db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                       content="hello", graph_sync_status="pending"))
    db.commit()
    facts_service.process_pending_memories(db, limit=5)
    db.expire_all()
    assert [m.graph_sync_status for m in db.query(AgentMemory).all()] == ["pending"]
    assert any(facts_drain._drain_paused(r) > 0 for r in list(facts_drain._pauses))


@pytest.mark.cloud
@pytest.mark.parametrize("status", [401, 403])
def test_the_hosted_alert_says_the_key_and_never_the_providers_words(monkeypatch, status):
    from korely_agent.hosted import alerts

    sent = []
    monkeypatch.setattr(alerts, "tell_the_founder", lambda subject, body: sent.append((subject, body)))
    words = "key revoked for the request 'my sister Anna lives in Turin'"
    alerts.writer_refused("eu", status, words)
    [(subject, body)] = sent
    assert "eu" in subject and "key" in subject and f"HTTP {status}" in body
    assert "Models page" in body and "payment" not in subject
    assert "Anna" not in subject + body and "revoked for the request" not in subject + body


@pytest.mark.cloud
def test_the_hosted_alert_for_payment_is_as_before(monkeypatch):
    from korely_agent.hosted import alerts

    sent = []
    monkeypatch.setattr(alerts, "tell_the_founder", lambda subject, body: sent.append((subject, body)))
    alerts.writer_refused("global", 402, "Your prepayment credits are depleted.")
    [(subject, body)] = sent
    assert subject == "The global writer is refused for payment"
    assert "HTTP 402 (payment required)" in body and "Top up" in body
