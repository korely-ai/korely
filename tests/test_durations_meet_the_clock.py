"""A duration setting where it meets a clock (8 Nov 2026).

Every value an install starts with starts, with the meaning it has
(tests/test_config.py): 0 or less is what each use made of it, a retention's
"for ever", a login's or a stale claim's floor. A value no clock holds made
a moment out of range where it was used: the purge failed every hour, the
sign-in, the reaper. There it is read as a century (core/times.
at_most_a_century), longer than anything this server keeps or waits for has
existed, and the log says so once.
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.core import times

HUGE_DAYS = 3_000_000


@pytest.fixture(autouse=True)
def _said_afresh():
    times._SAID.clear()
    yield
    times._SAID.clear()


def test_a_century_or_the_value(caplog):
    with caplog.at_level(logging.WARNING):
        assert times.at_most_a_century("audit_retention_days", 400, 1) == 400
        assert times.at_most_a_century("audit_retention_days", -1, 1) == -1
        assert times.at_most_a_century("audit_retention_days", HUGE_DAYS, 1) == 36_500
        assert times.at_most_a_century("audit_retention_days", HUGE_DAYS, 1) == 36_500
        assert times.at_most_a_century("session_hours", 10 ** 9, 24) == 36_500 * 24
    said = [r.getMessage() for r in caplog.records if "longer than a century" in r.getMessage()]
    assert said == [f"AUDIT_RETENTION_DAYS={HUGE_DAYS} is longer than a century: read as a "
                    "century, which this server never reaches",
                    f"SESSION_HOURS={10 ** 9} is longer than a century: read as a century, "
                    "which this server never reaches"], "once per setting"


@pytest.mark.parametrize("keep", [-1, 0], ids=["minus-one", "zero"])
def test_a_retention_of_zero_or_less_keeps_everything(db, keep):
    from korely_agent.services import audit, llm_usage, webhooks

    for purge in (audit.purge_expired, llm_usage.purge_expired, webhooks.purge_expired):
        assert purge(db, keep) == 0


def test_a_retention_no_clock_holds_purges_nothing_and_fails_nothing(db, caplog):
    from korely_agent.services import audit, llm_usage, read_marks, webhooks

    with caplog.at_level(logging.WARNING):
        for purge in (audit.purge_expired, llm_usage.purge_expired, webhooks.purge_expired,
                      read_marks.purge_expired):
            assert purge(db, HUGE_DAYS) == 0
            assert purge(db, 40_000) == 0, "40,000 days: what it meant, nothing that old"
    assert sum("longer than a century" in r.getMessage() for r in caplog.records) == 4


def test_a_login_of_zero_hours_lasts_one_and_one_of_a_billion_a_century(db, tenant):
    from korely_agent.services import operators as ops

    op, _password = ops.create(db, tenant_id=tenant.id, username="anna")
    now = datetime.now(timezone.utc)
    short, _ = ops.open_session(db, op, hours=0)
    assert timedelta(minutes=59) < short.expires_at - now < timedelta(minutes=61)
    long_, _ = ops.open_session(db, op, hours=10 ** 9)
    assert timedelta(days=36_499) < long_.expires_at - now < timedelta(days=36_501)


def test_a_stale_claim_of_zero_minutes_is_one_and_one_no_clock_holds_is_a_century(
        db, tenant, monkeypatch):
    from korely_agent.config import get_settings
    from korely_agent.db.models import AgentBatchJob
    from korely_agent.services import batch
    from korely_agent.services.facts import drain

    job = batch.enqueue_batch(db, tenant.id, [{"content": "memory 0", "user_id": "u1"}])
    db.query(AgentBatchJob).filter(AgentBatchJob.id == job.id).update(
        {"status": batch.STATUS_PROCESSING,
         "updated_at": datetime.now(timezone.utc) - timedelta(minutes=2)})
    db.commit()
    try:
        for minutes, reclaimed in ((10 ** 10, 0), (0, 1)):
            monkeypatch.setenv("DRAIN_STALE_MINUTES", str(minutes))
            get_settings.cache_clear()
            assert batch.reclaim_interrupted(db) == reclaimed, minutes
        # The memories' claim: 0 as before (memory_core's claim keeps its
        # floor of a minute), a value no clock holds as a century.
        assert drain._stale_minutes() == 0
        monkeypatch.setenv("DRAIN_STALE_MINUTES", str(10 ** 10))
        get_settings.cache_clear()
        assert drain._stale_minutes() == 36_500 * 24 * 60
    finally:
        get_settings.cache_clear()


def test_a_model_call_waits_what_the_setting_says_up_to_a_century(monkeypatch):
    from korely_agent.config import get_settings
    from korely_agent.core import llm

    for value, waited in ((0, 0), (60, 60), (10 ** 12, 36_500 * 86_400)):
        monkeypatch.setenv("LLM_TIMEOUT_SECONDS", str(value))
        get_settings.cache_clear()
        assert llm._model_call_seconds() == waited
    get_settings.cache_clear()
