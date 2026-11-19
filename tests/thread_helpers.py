"""What the tests read of core/threads' tables, which the product never
reads: kept here, not as methods of the classes (audit of 27 Oct 2026).
`wait_idle` is the product's too, and only calls it."""
from __future__ import annotations


def held_now(places) -> dict:
    """{key: places held} of a core/threads.Places, a copy."""
    with places._lock:
        return dict(places._held)


def wait_idle(lanes, seconds: float) -> bool:
    """Whether every job of a core/threads.Lanes ended within `seconds`: a
    test that must see every outcome written. The facts drain waits the same
    way (Lanes.wait_idle, its caller in the product)."""
    return lanes.wait_idle(seconds)


def wait_account_idle(lanes, account, seconds: float) -> bool:
    """Whether the thread of every job of `account` in a core/threads.Lanes
    ended within `seconds`, while other accounts' jobs may still run: a test
    that must see one account's outcomes written while another's are held.
    Lanes.wait_idle's sense, for one account (`_alive`): a job given up on
    still counts until its thread ends."""
    with lanes._cv:
        return lanes._cv.wait_for(lambda: account not in lanes._alive.values(), seconds)
