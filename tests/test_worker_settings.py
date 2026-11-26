"""What each background job takes per tick, and how long it keeps, is a setting.

The facts drain read `drain_limit` and the audit and usage purges read their
retentions, while the batch drain took 5, the webhook drain 20 and the delivery
purge 30 days, typed into worker.py and webhooks.py where an operator could not
reach them. The defaults are unchanged; what changed is who can move them.

No database: the jobs are replaced by recorders, and what is checked is the
number the worker hands them.
"""
import pytest

from korely_agent import worker

DB = "postgresql://u:p@localhost:5432/korely"


@pytest.fixture
def configured(monkeypatch):
    """Settings read from the environment, as an install reads its .env."""
    from korely_agent.config import get_settings

    monkeypatch.setenv("DATABASE_URL", DB)

    def use(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        get_settings.cache_clear()

    yield use
    get_settings.cache_clear()


def _recorder(calls, name, returns):
    def record(*args, **kwargs):
        calls[name] = (args[1:], kwargs)
        return returns
    return record


class TestTheDefaultsDidNotMove:
    def test_same_numbers_as_before(self):
        from korely_agent.config import AgentSettings

        s = AgentSettings(database_url=DB)
        assert (s.drain_limit, s.batch_drain_limit, s.webhook_drain_limit) == (20, 5, 20)
        assert (s.audit_retention_days, s.llm_usage_retention_days,
                s.webhook_delivery_retention_days) == (400, 400, 30)


class TestTheWorkerReadsThem:
    def test_the_batch_drain(self, configured, monkeypatch):
        from korely_agent.services import batch

        calls = {}
        monkeypatch.setattr(batch, "process_batch_jobs", _recorder(calls, "batch", {}))
        configured(BATCH_DRAIN_LIMIT=7)
        worker._batch(object())
        assert calls["batch"][1] == {"limit": 7}

    def test_the_webhook_drain(self, configured, monkeypatch):
        from korely_agent.services import webhooks

        calls = {}
        monkeypatch.setattr(webhooks, "process_deliveries", _recorder(calls, "webhooks", {}))
        configured(WEBHOOK_DRAIN_LIMIT=9)
        worker._webhooks(object())
        assert calls["webhooks"][1] == {"limit": 9}

    def test_the_purge(self, configured, monkeypatch):
        from korely_agent.services import (audit, batch, idempotency, llm_usage, oauth,
                                           operators, rate_limit, read_marks, webhooks)

        calls = {}
        for module, name in ((rate_limit, "rate_limit"), (audit, "audit"),
                             (llm_usage, "llm_usage"), (operators, "sessions"),
                             (webhooks, "deliveries"), (oauth, "oauth"),
                             (read_marks, "read_marks"), (idempotency, "idempotency_keys")):
            monkeypatch.setattr(module, "purge_expired", _recorder(calls, name, 0))
        monkeypatch.setattr(batch, "purge_finished", _recorder(calls, "batches", 0))
        from korely_agent.services import keys

        monkeypatch.setattr(keys, "purge_revoked", _recorder(calls, "api_keys", 0))
        configured(AUDIT_RETENTION_DAYS=11, LLM_USAGE_RETENTION_DAYS=12,
                   WEBHOOK_DELIVERY_RETENTION_DAYS=13, READ_MARKS_RETENTION_DAYS=14)
        worker._purge(object())
        assert calls["audit"][0] == (11,)
        assert calls["llm_usage"][0] == (12,)
        assert calls["deliveries"][0] == (13,), \
            "finished webhook deliveries are still kept for a number nobody can set"
        assert calls["read_marks"][0] == (14,), "the read marks are kept for the setting's days"
        # Finished batch jobs on their fixed window (batch.FINISHED_JOB_RETENTION),
        # not on a setting: the purge is called with nothing to override it.
        assert calls["batches"] == ((), {}), "finished /v1/batch jobs are never purged"
        # OAuth's spent grants and idle clients (services/oauth.py): a fixed day.
        assert calls["oauth"] == ((), {}), "spent OAuth grants are never purged"
        # The Idempotency-Keys past their 24 hours (services/idempotency.LIFETIME).
        assert calls["idempotency_keys"] == ((), {}), "expired Idempotency-Keys are never purged"
        # The keys revoked past the trail's retention (services/keys.purge_revoked,
        # which reads the setting itself), since 1.0.40.
        assert "api_keys" in calls, "revoked API keys are never purged"

    def test_zero_still_means_forever_for_deliveries(self):
        """The same convention as the other two retentions: 0 deletes nothing,
        and returns before it would touch the database (hence `None`)."""
        from korely_agent.services import webhooks

        assert webhooks.purge_expired(None, 0) == 0

    def test_one_record_that_fails_does_not_stop_the_others(self, configured, monkeypatch):
        """Each record is trimmed on its own: a retention no clock could hold
        stopped the purge at that record for every record after it (review of
        piece 1 of agent notices, 8 Nov 2026). The first failure is raised once
        every record had its turn, so the job reads failing on /health."""
        from korely_agent.services import (audit, batch, idempotency, llm_usage, oauth,
                                           operators, rate_limit, read_marks, webhooks)

        calls = {}
        for module, name in ((rate_limit, "rate_limit"), (llm_usage, "llm_usage"),
                             (operators, "sessions"), (webhooks, "deliveries"), (oauth, "oauth"),
                             (read_marks, "read_marks"), (idempotency, "idempotency_keys")):
            monkeypatch.setattr(module, "purge_expired", _recorder(calls, name, 0))
        monkeypatch.setattr(batch, "purge_finished", _recorder(calls, "batches", 0))
        from korely_agent.services import keys

        monkeypatch.setattr(keys, "purge_revoked", _recorder(calls, "api_keys", 0))

        def refused(*a, **k):
            raise OverflowError("date value out of range")

        monkeypatch.setattr(audit, "purge_expired", refused)
        configured()

        class Session:
            rolled_back = 0

            def rollback(self):
                Session.rolled_back += 1

        with pytest.raises(OverflowError):
            worker._purge(Session())
        assert set(calls) == {"rate_limit", "llm_usage", "sessions", "deliveries", "read_marks",
                              "batches", "idempotency_keys", "oauth", "api_keys"}, \
            "a record after the failing one was skipped"
        assert Session.rolled_back == 1
        worker._job_state.pop("purge", None)
        worker._run(lambda db: worker._purge(Session()), "purge")
        assert worker.jobs_state()["purge"]["state"] == "failing"
