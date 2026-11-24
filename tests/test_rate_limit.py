"""The ceiling that protects the operator's Postgres and model bill.

A rate limiter is only ever exercised under concurrency, which is exactly the
condition its own implementation has to survive. These tests use real threads on
real connections, because a limiter tested one request at a time is tested in
the one situation it was not written for.
"""
import threading
import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from korely_agent.db.models import AgentRateLimit, Tenant
from korely_agent.services.rate_limit import (
    RateLimited,
    check_and_increment,
    purge_expired,
)



@pytest.fixture
def limit_on(monkeypatch):
    def _set(n):
        monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", str(n))
        from korely_agent import config

        config.get_settings.cache_clear()
        return n
    yield _set
    from korely_agent import config

    config.get_settings.cache_clear()


class TestOneRequestAtATime:
    def test_off_by_default_reports_unlimited(self, db, tenant):
        info = check_and_increment(db, tenant.id)
        assert info.limit == 0

    def test_counts_down_to_the_ceiling(self, db, tenant, limit_on):
        limit_on(3)
        assert check_and_increment(db, tenant.id).remaining == 2
        assert check_and_increment(db, tenant.id).remaining == 1
        assert check_and_increment(db, tenant.id).remaining == 0
        with pytest.raises(RateLimited) as e:
            check_and_increment(db, tenant.id)
        assert e.value.limit == 3
        assert 0 <= e.value.reset_in <= 60

    def test_one_tenant_does_not_spend_anothers_allowance(self, db, limit_on):
        limit_on(2)
        a = Tenant(id=uuid.uuid4(), name="A")
        b = Tenant(id=uuid.uuid4(), name="B")
        db.add_all([a, b])
        db.commit()

        check_and_increment(db, a.id)
        check_and_increment(db, a.id)
        with pytest.raises(RateLimited):
            check_and_increment(db, a.id)
        # B is untouched
        assert check_and_increment(db, b.id).remaining == 1

    def test_purge_removes_only_old_buckets(self, db, tenant, limit_on):
        limit_on(100)
        check_and_increment(db, tenant.id)
        assert purge_expired(db, keep_minutes=120) == 0
        assert db.query(AgentRateLimit).count() == 1

        # A bucket from a window that closed long ago. keep_minutes=0 does not
        # delete the current one: "older than now" is false for the minute we
        # are still inside, which is the correct reading and worth pinning.
        db.add(AgentRateLimit(tenant_id=tenant.id, window_type="minute",
                              bucket="202001010000", count=7))
        db.commit()
        assert purge_expired(db, keep_minutes=0) == 1
        assert db.query(AgentRateLimit).count() == 1

    def test_the_hour_and_day_rows_go_two_hours_after_their_window(self, db, tenant):
        """The privacy policy (§9) says about two hours for the minute, hour
        and day counters; the hour and day rows stayed two days (audit of 17
        Oct 2026). A row goes once its window ended `keep_minutes` ago."""
        from datetime import timedelta

        from korely_agent.services import rate_limit
        from korely_agent.services.rate_limit import bucket_key

        now = rate_limit.clock()   # the purge's clock, held by tests/conftest.py
        rows = {
            # Ended two hours ago or more, whatever the time of day.
            ("hour", bucket_key(now - timedelta(hours=3), "hour")): False,
            ("day", bucket_key(now - timedelta(hours=26), "day")): False,
            # Still running, or ended less than two hours ago.
            ("hour", bucket_key(now - timedelta(hours=1), "hour")): True,
            ("hour", bucket_key(now, "hour")): True,
            ("day", bucket_key(now, "day")): True,
        }
        db.add_all([AgentRateLimit(tenant_id=tenant.id, window_type=w, bucket=b, count=1)
                    for w, b in rows])
        db.commit()
        assert purge_expired(db, keep_minutes=120) == 2
        left = {(r.window_type, r.bucket) for r in db.query(AgentRateLimit).all()}
        assert left == {k for k, kept in rows.items() if kept}

    def test_every_door_and_every_key_length_goes_by_the_same_rule(self, db, tenant):
        """A bucket of any door (the account's, the control plane's, one
        another module counts in) and keyed by any length of the moment, down
        to the second (`INSTANT`), goes once its window ended `keep_minutes`
        ago. Every door used to be listed in the purge, and a door added
        later kept its rows for ever."""
        from datetime import timedelta

        from korely_agent.services import rate_limit
        from korely_agent.services.rate_limit import INSTANT, bucket_key

        now = rate_limit.clock()
        rows = {
            ("x:minute", bucket_key(now - timedelta(hours=3), "minute")): False,
            ("x:minute", bucket_key(now - timedelta(minutes=30), "minute")): True,
            ("a:hour", bucket_key(now - timedelta(hours=3), "hour")): False,
            ("x:moment", (now - timedelta(minutes=121)).strftime(INSTANT)): False,
            ("x:moment", (now - timedelta(minutes=119)).strftime(INSTANT)): True,
        }
        db.add_all([AgentRateLimit(tenant_id=tenant.id, window_type=w, bucket=b, count=1)
                    for w, b in rows])
        db.commit()
        assert purge_expired(db, keep_minutes=120) == 3
        left = {(r.window_type, r.bucket) for r in db.query(AgentRateLimit).all()}
        assert left == {k for k, kept in rows.items() if kept}

    def test_a_ceiling_with_its_own_sentence_says_it_in_the_common_429(self):
        from korely_agent.core.auth import rate_limited

        e = rate_limited(RateLimited(limit=5, reset_in=42, message="Wait 42 seconds."))
        assert e.status_code == 429 and e.detail == "Wait 42 seconds."
        assert e.headers["Retry-After"] == "42" and e.code == "rate_limit_exceeded"
        plain = rate_limited(RateLimited(limit=2, reset_in=7))
        assert plain.detail == "Rate limit exceeded (2/min): try again in 7 s"


class TestUnderConcurrency:
    """The situation a limiter exists for."""

    def _hammer(self, engine, tenant_id, n_threads):
        Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        errors, limited = [], []
        start = threading.Barrier(n_threads)

        def one():
            s = Session()
            try:
                start.wait(timeout=10)
                check_and_increment(s, tenant_id)
            except RateLimited:
                limited.append(1)
            except Exception as e:            # anything else is a defect
                errors.append(e)
            finally:
                s.rollback()
                s.close()

        threads = [threading.Thread(target=one) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        return errors, len(limited)

    def test_the_first_requests_of_a_new_minute_do_not_collide(
        self, db, engine, tenant, limit_on
    ):
        """Every minute has a first request. When two arrive together they both
        find no counter row and both INSERT, and the primary key on
        (tenant_id, window_type, bucket) rejects the loser. Uncaught, that is a
        500 on a request that was inside its allowance.
        """
        limit_on(1000)
        errors, _ = self._hammer(engine, tenant.id, n_threads=8)
        assert not errors, f"concurrent first requests raised: {errors[:3]}"

    def test_no_increment_is_lost(self, db, engine, tenant, limit_on):
        """`count = count + 1` in Python is read-modify-write. Two requests that
        read the same value both write the same result, one increment vanishes,
        and the ceiling leaks by however many requests overlapped.
        """
        limit_on(1000)
        n = 12
        errors, _ = self._hammer(engine, tenant.id, n_threads=n)
        assert not errors, errors[:3]

        db.rollback()
        rows = db.query(AgentRateLimit).all()
        assert len(rows) == 1, "one row per minute (the minute is held, tests/conftest.py)"
        assert sum(r.count for r in rows) == n, (
            f"counted {sum(r.count for r in rows)} of {n} requests"
        )
