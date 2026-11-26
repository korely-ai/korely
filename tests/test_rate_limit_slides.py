"""A ceiling that slides (rate_limit.Ceiling.sliding, rate_limit.in_the_last):
the last 60 seconds back from now, to the second, never the clock's minute.

The clock's minute starts again at its first second: a ceiling of 20 let 20
through at hh:mm:59 and 20 more at hh:mm+1:00, 40 in about a second (the
hosted trial's minute slides since 26 Nov 2026, as the large model APIs
count). Sliding, the 21st of any 60 seconds is refused, and its
Retry-After is the moment the window has room again, not the clock's next
minute."""
import threading
from datetime import timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from korely_agent.core.extensions import hooks
from korely_agent.db.models import AgentRateLimit
from korely_agent.services import rate_limit
from korely_agent.services.rate_limit import Ceiling, RateLimited, check_and_increment


@pytest.fixture
def clock(monkeypatch, _the_minute_holds):
    """The limiter's clock, moved by the test: `clock.go(seconds)`; it starts
    half way through a minute (tests/conftest.py)."""

    class Clock:
        now = _the_minute_holds

        def go(self, seconds):
            self.now += timedelta(seconds=seconds)

    c = Clock()
    monkeypatch.setattr(rate_limit, "clock", lambda: c.now)
    return c


@pytest.fixture
def sliding(monkeypatch):
    """The account's minute as a ceiling that slides, through the hook a
    hosted plan gives its windows by (core/extensions.rate_limits)."""
    def _set(n, **more):
        monkeypatch.setattr(hooks, "rate_limits",
                            lambda db, t: {"minute": Ceiling(n, sliding=True), **more})
        return n
    return _set


def _refused(db, tenant_id):
    with pytest.raises(RateLimited) as e:
        check_and_increment(db, tenant_id)
    return e.value


def test_the_turn_of_the_clocks_minute_lets_none_more_through(db, tenant, clock, sliding):
    """20 at hh:mm:59, then the clock's minute turns: the 21st is refused,
    and told the time its window truly has left (59 s), not the 60 of a new
    minute. On the clock's minute it was let through, with 19 more."""
    sliding(20)
    clock.go(29)  # hh:mm:59
    for _ in range(20):
        check_and_increment(db, tenant.id)
    clock.go(1)  # hh:mm+1:00
    e = _refused(db, tenant.id)
    assert e.limit == 20 and e.window == "minute"
    assert e.reset_in == 59
    # Waiting what it was told is enough, and not a second less.
    clock.go(58)
    assert _refused(db, tenant.id).reset_in == 1
    clock.go(1)
    assert check_and_increment(db, tenant.id).remaining == 19


def test_the_window_frees_as_its_oldest_leave(db, tenant, clock, sliding):
    """Ten at :30 and ten at :50: at :59 the window is full, and the first
    place frees when the ten of :30 are 60 seconds old (31 s), not at the
    clock's next minute (1 s)."""
    sliding(20)
    for _ in range(10):
        check_and_increment(db, tenant.id)
    clock.go(20)
    for _ in range(10):
        check_and_increment(db, tenant.id)
    clock.go(9)
    assert _refused(db, tenant.id).reset_in == 31
    clock.go(31)
    # The ten of :30 left: ten places, then the ten of :50 are the oldest.
    assert [check_and_increment(db, tenant.id).remaining for _ in range(10)][-1] == 0
    assert _refused(db, tenant.id).reset_in == 20


def test_a_ceiling_lowered_with_rows_in_it_waits_for_the_row_that_makes_room(
        db, tenant, clock, sliding):
    """A wait is the moment the row whose leaving makes room leaves, not the
    oldest's: 5 at :30, 5 at :40 and a ceiling lowered to 4 has room once
    both are gone (at :40 + 60 s)."""
    sliding(10)
    for _ in range(5):
        check_and_increment(db, tenant.id)
    clock.go(10)
    for _ in range(5):
        check_and_increment(db, tenant.id)
    sliding(4)
    clock.go(5)
    assert _refused(db, tenant.id).reset_in == 55


def test_a_refused_request_spends_no_place(db, tenant, clock, sliding):
    sliding(2)
    check_and_increment(db, tenant.id)
    check_and_increment(db, tenant.id)
    for _ in range(5):
        _refused(db, tenant.id)
    db.expire_all()
    rows = db.query(AgentRateLimit).filter(AgentRateLimit.tenant_id == tenant.id).all()
    assert [(r.window_type, r.count) for r in rows] == [(rate_limit.sliding("minute"), 2)]


def test_a_request_another_window_refuses_takes_no_place_in_the_sliding_one(
        db, tenant, clock, sliding):
    """The windows of one request are counted together and given back
    together: refused by the clock's hour, it spends nothing of the minute."""
    sliding(10, hour=2)
    check_and_increment(db, tenant.id)
    check_and_increment(db, tenant.id)
    e = _refused(db, tenant.id)
    assert e.window == "hour"
    db.expire_all()
    slid = db.query(AgentRateLimit).filter(
        AgentRateLimit.tenant_id == tenant.id,
        AgentRateLimit.window_type == rate_limit.sliding("minute")).one()
    assert slid.count == 2


def test_the_sliding_rows_go_with_the_limiters_purge(db, tenant, clock, sliding):
    sliding(5)
    check_and_increment(db, tenant.id)
    clock.go(2 * 3600 + 1)
    assert rate_limit.purge_expired(db) == 1


def test_two_requests_at_once_on_the_last_place_across_the_clocks_minute(
        db, engine, tenant, clock, sliding):
    """19 at hh:mm:40, then two at once at hh:mm+1:00: one place is left in
    the last 60 seconds, so one is let in and one refused. On the clock's
    minute both were let in (a new minute), 21 in 20 seconds; without the
    account's lock both could read 19 and both be counted."""
    sliding(20)
    clock.go(10)
    for _ in range(19):
        check_and_increment(db, tenant.id)
    clock.go(20)
    db.commit()

    Session = sessionmaker(bind=engine)
    start, let_in, refused, errors = threading.Barrier(2), [], [], []

    def one():
        s = Session()
        try:
            start.wait(timeout=10)
            check_and_increment(s, tenant.id)
            let_in.append(1)
        except RateLimited as e:
            refused.append(e.reset_in)
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)
        finally:
            s.close()

    threads = [threading.Thread(target=one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert len(let_in) == 1 and refused == [40], (let_in, refused)
    db.expire_all()
    assert sum(r.count for r in db.query(AgentRateLimit).filter(
        AgentRateLimit.tenant_id == tenant.id)) == 20


def test_many_at_once_never_pass_the_ceiling(db, engine, tenant, clock, sliding):
    """Twelve at once on a ceiling of five: five let in, seven refused,
    five counted."""
    sliding(5)
    Session = sessionmaker(bind=engine)
    start, let_in, refused, errors = threading.Barrier(12), [], [], []

    def one():
        s = Session()
        try:
            start.wait(timeout=10)
            check_and_increment(s, tenant.id)
            let_in.append(1)
        except RateLimited:
            refused.append(1)
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)
        finally:
            s.close()

    threads = [threading.Thread(target=one) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert (len(let_in), len(refused)) == (5, 7)


def test_a_wait_of_a_fraction_of_a_second_is_rounded_up(db, tenant, clock, sliding):
    """Two at :30, refused at :40.4: the window has 49.6 s left, told 50, so
    a caller that waits what it is told is let in (the skeptic's M05)."""
    sliding(2)
    check_and_increment(db, tenant.id)
    check_and_increment(db, tenant.id)
    clock.go(10.4)
    assert _refused(db, tenant.id).reset_in == 50
    clock.go(49.6)
    check_and_increment(db, tenant.id)


def test_a_ceiling_lowered_to_what_its_rows_hold_waits_for_the_row_that_makes_room(
        db, tenant, clock, sliding):
    """2 at :30 and 2 at :40, the ceiling lowered from 4 to 2: once the two
    of :30 leave, the two of :40 still fill it, so the room is at :40 + 60
    s, not at :30 + 60 s (the skeptic's M07: the one asking is counted)."""
    sliding(4)
    check_and_increment(db, tenant.id)
    check_and_increment(db, tenant.id)
    clock.go(10)
    check_and_increment(db, tenant.id)
    check_and_increment(db, tenant.id)
    sliding(2)
    clock.go(5)
    assert _refused(db, tenant.id).reset_in == 55


def test_a_refusal_counts_nothing_in_the_window(db, tenant, clock):
    """`in_the_last` on its own (a part's windows call it with no
    savepoint around it): a refusal adds no row (the skeptic's M11)."""
    for _ in range(5):
        rate_limit.in_the_last(db, tenant.id, "~min", 2, timedelta(minutes=1))
    db.commit()
    db.expire_all()
    rows = db.query(AgentRateLimit).filter(AgentRateLimit.tenant_id == tenant.id).all()
    assert sum(r.count for r in rows) == 2


@pytest.mark.parametrize("back", [5, 3600], ids=["5s", "1h"])
def test_with_the_clock_set_back_the_wait_is_never_longer_than_the_window(
        db, tenant, clock, sliding, back):
    """Rows of a moment the clock has not reached again are in the window:
    the wait they would give (65 s, 3,660 s) is not one to tell; at most
    the window's 60 (the skeptic's P2)."""
    sliding(3)
    for _ in range(3):
        check_and_increment(db, tenant.id)
    clock.go(-back)
    assert _refused(db, tenant.id).reset_in == 60


def test_a_window_longer_than_the_rows_are_kept_is_refused(db, tenant, clock, monkeypatch):
    """The purge keeps a row two hours past its window (rate_limit.KEPT):
    a sliding day would lose its rows before they leave it, and let through
    what it was meant to refuse."""
    with pytest.raises(ValueError):
        rate_limit.in_the_last(db, tenant.id, "~day", 5, timedelta(days=1))
    db.rollback()
    assert rate_limit.in_the_last(db, tenant.id, "~hour", 5, rate_limit.KEPT).count == 1
    db.rollback()
    monkeypatch.setattr(hooks, "rate_limits", lambda db, t: {"day": Ceiling(5, sliding=True)})
    with pytest.raises(ValueError):
        check_and_increment(db, tenant.id)
