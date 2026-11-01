"""Times as they come back from the database, one rule (2026-10-06).

There were three copies of it (`manage.py`, `services/keys.py`,
`services/operators.py`; the code audit, row 15), each explaining the naive
time differently.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Read back from Postgres a time has its zone; one set in Python on an
    object not yet reloaded may be naive, and a naive time here is UTC. None
    stays None (since 18 Oct 2026, when the rule written again in seven places
    came here: the code audit of 17 Oct, R2-DUP-01)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
