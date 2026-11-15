"""Request rate limiting.

The version this replaces implemented a hosted pricing model: four tiers with
euro prices, monthly write and query quotas, and a cap on how many distinct
agents an account could register. None of that belongs in software you run
yourself. You pay your own model bill and your own database, so there is nothing
to meter and nobody to bill.

What survives is the part that stays useful when you host it: a ceiling that
protects your Postgres and your model spend from one runaway agent in a retry
loop. Off by default, because the operator is the only person who knows what
their hardware can take.

Fixed-window counters in Postgres, one row per (tenant, window, bucket). No
Redis: at the request rates a single instance serves, a table is faster to
operate than another service to run.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..config import settings
from ..db import AgentRateLimit


@dataclass(frozen=True)
class RateInfo:
    limit: int
    remaining: int
    reset_in: int


class RateLimited(Exception):
    """Raised when a tenant exceeds the configured ceiling.

    Carries what a 429 needs, so the API layer can build the reply without
    knowing how the counter works. `message`: the sentence of a ceiling
    whose reason and way on are its own (a bucket another module counts in
    this table); without it the 429 names the ceiling and its window.
    """

    def __init__(self, *, limit: int, reset_in: int, window: str = "minute",
                 message: Optional[str] = None):
        self.limit = limit
        self.reset_in = reset_in
        self.window = window
        self.message = message
        super().__init__(f"rate limit of {limit}/{UNITS[window]} exceeded, resets in {reset_in}s")


# How a window is written in a 429 ("60/min"). The hosted service has the hour
# and the day as well (its plans' windows, korely_agent/hosted/); an install of
# one's own counts the minute only.
UNITS = {"minute": "min", "hour": "hour", "day": "day"}
_FORMATS = {"minute": "%Y%m%d%H%M", "hour": "%Y%m%d%H", "day": "%Y%m%d"}
# The finest key a bucket has: the second. Every window's key above is a
# beginning of it, so one rule purges every bucket (`purge_expired`); a row
# keyed by the second it was written (a moment, not a window) uses it.
INSTANT = "%Y%m%d%H%M%S"


_UNLIMITED = RateInfo(limit=0, remaining=0, reset_in=0)


def clock() -> datetime:
    """The limiter's clock, one name the tests hold still (tests/conftest.py,
    `the_minute_holds`): a test that counted four calls against a ceiling of
    two failed whenever the minute turned between them (audit of 17 Oct 2026)."""
    return datetime.now(timezone.utc)


def bucket_key(now: datetime, window: str = "minute") -> str:
    """The key of the bucket of `window` that `now` falls in."""
    return now.strftime(_FORMATS[window])


def _reset_in(now: datetime, window: str) -> int:
    if window == "minute":
        return 60 - now.second
    if window == "hour":
        return 3600 - (now.minute * 60 + now.second)
    return 86400 - (now.hour * 3600 + now.minute * 60 + now.second)


def network_of(ip: Optional[str]) -> Optional[str]:
    """The network a caller without a key counts against: its address, its
    /64 for IPv6 (one home or one server is handed a whole /64). The hosted
    sign-up's accounts per network in a day (hosted/signup.py) and OAuth's
    registrations and requests per network in an hour (services/oauth.py)."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip[:64]
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


# The control plane's bucket: a person signed in to the dashboard counts the
# account's ceiling in a bucket of their own, its window types prefixed with
# this (`window_type` is eight characters: "a:minute" fits). The audit of 20
# Oct 2026 found the control plane shared the keys' bucket: any
# key of an account, a leaked read-only one included, spent the one bucket
# the operator's POST /admin/keys/{id}/revoke drew on, and kept the key live
# by keeping its revocation refused.
CONTROL_PLANE = "a:"


def add_one(db: Session, tenant_id: UUID, window_type: str, bucket: str) -> int:
    """Count one in the row (tenant, window_type, bucket), made if it is not
    there, and return the count this request got: one statement, for the
    reasons in `check_and_increment`. The building block of every bucket of
    this table, the account's, the control plane's and any other module's."""
    return add(db, tenant_id, window_type, bucket, 1)


def add(db: Session, tenant_id: UUID, window_type: str, bucket: str, amount: int) -> int:
    """`add_one` for `amount` at once (a bucket that counts more than one
    thing a time): the row's count after it."""
    stmt = (
        pg_insert(AgentRateLimit)
        .values(tenant_id=tenant_id, window_type=window_type, bucket=bucket, count=amount)
        .on_conflict_do_update(
            index_elements=[
                AgentRateLimit.__table__.c.tenant_id,
                AgentRateLimit.__table__.c.window_type,
                AgentRateLimit.__table__.c.bucket,
            ],
            set_={"count": AgentRateLimit.__table__.c.count + amount},
        )
        .returning(AgentRateLimit.__table__.c.count)
    )
    return db.execute(stmt).scalar_one()


def check_and_increment(db: Session, tenant_id: UUID, *, door: str = "") -> RateInfo:
    """Count this request and report what is left in the current minute.

    Returns an unlimited RateInfo when limiting is off, so callers can set
    response headers without branching on configuration.

    `door`: the bucket, the account's (every key, every MCP call) or the
    control plane's (`CONTROL_PLANE`, a signed-in person's), with the same
    ceilings. A refused request spends no window: its increments are rolled
    back, so a sender past the minute keeps no longer window exhausted with
    calls it was never served (audit of 20 Oct 2026, the same finding).
    """
    # The hosted service gives each account its plan's windows (minute, hour,
    # day) through a hook; an install of one's own has the one number below.
    from ..core.extensions import hooks

    ceilings = hooks.rate_limits(db, tenant_id)
    if ceilings is None:
        limit = int(settings.rate_limit_per_minute or 0)
        if limit <= 0:
            return _UNLIMITED
        ceilings = {"minute": limit}
    ceilings = {w: int(n) for w, n in ceilings.items() if n and int(n) > 0}
    if not ceilings:
        return _UNLIMITED

    now = clock()

    # One statement, on purpose. The obvious version reads the row, adds one in
    # Python and writes it back, and it is wrong twice under the only condition
    # a rate limiter ever runs in:
    #
    #   * every minute has a first request. Two arriving together both find no
    #     row and both INSERT, and the primary key on
    #     (tenant_id, window_type, bucket) rejects the loser. Uncaught, that is
    #     a 500 on a request that was inside its allowance - and it appears
    #     only under load, which is when you least want to be reading a
    #     traceback.
    #   * `count = count + 1` in Python is read-modify-write. Two requests that
    #     read the same value write the same result, one increment disappears,
    #     and the ceiling leaks by however many requests overlapped.
    #
    # ON CONFLICT DO UPDATE with the arithmetic inside the statement has neither
    # problem: the row is created or incremented atomically, and RETURNING gives
    # back the value this request actually got.
    counts = {}
    counted = db.begin_nested()
    for window in ceilings:
        counts[window] = add_one(db, tenant_id, door + window, bucket_key(now, window))
    refused = next((w for w, limit in ceilings.items() if counts[w] > limit), None)
    if refused is not None:
        counted.rollback()
    db.commit()
    if refused is not None:
        raise RateLimited(limit=ceilings[refused], reset_in=_reset_in(now, refused),
                          window=refused)

    tightest = None
    for window, limit in ceilings.items():
        count, reset_in = counts[window], _reset_in(now, window)
        info = RateInfo(limit=limit, remaining=max(0, limit - count), reset_in=reset_in)
        if tightest is None or info.remaining < tightest.remaining:
            tightest = info
    return tightest


def purge_expired(db: Session, keep_minutes: int = 120) -> int:
    """Delete the counter rows whose window ended `keep_minutes` ago or more.

    One row per tenant per window, useless the moment its window passes.
    Without this the table grows forever, which is a slow failure and therefore
    the easy one to miss until someone's disk fills at two in the morning.

    One rule for every window: a row goes once `keep_minutes` have passed since
    its minute, its hour or its UTC day ended (the worker purges hourly). With
    the default, a minute or an hour row goes about two hours after its window,
    a day row about two hours after its UTC day, 26 hours from its start at
    most. The hour and day rows of the hosted plans were kept two days, while
    the privacy policy (§9) said about two hours (audit of 17 Oct 2026); an
    install of one's own writes minute rows only.

    Whatever the door and the window: a bucket's key is the beginning of the
    second it is in (`INSTANT`) as long as its window is, so a bucket before
    the one `cutoff` falls in, at the bucket's own length, ended at or before
    `cutoff`. Every door used to be listed here, and a bucket of a door added
    later stayed for ever.
    """
    cutoff = (clock() - timedelta(minutes=keep_minutes)).strftime(INSTANT)
    bucket = AgentRateLimit.bucket
    n = (db.query(AgentRateLimit)
           .filter(bucket < func.left(cutoff, func.length(bucket)))
           .delete(synchronize_session=False))
    db.commit()
    return n
