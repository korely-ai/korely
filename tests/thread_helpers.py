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
