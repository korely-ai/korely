"""Times as they come back from the database, one rule (2026-10-06).

There were three copies of it (`manage.py`, `services/keys.py`,
`services/operators.py`; the code audit, row 15), each explaining the naive
time differently. And how long a duration setting may reach, where it is
added to now or taken from it (`at_most_a_century`).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

#: The longest a duration setting reaches where it meets a clock: a century,
#: in days. Longer than anything this server keeps or waits for has existed
#: (the product began in 2026), so a longer value means what it meant, for
#: ever, and every clock it meets holds it: Python's dates end in year 9999
#: and begin in year 1, a socket's timeout ends near 292 years.
CENTURY_DAYS = 36_500
_SAID: set = set()


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Read back from Postgres a time has its zone; one set in Python on an
    object not yet reloaded may be naive, and a naive time here is UTC. None
    stays None (since 18 Oct 2026, when the rule written again in seven places
    came here: the code audit of 17 Oct, R2-DUP-01)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def at_most_a_century(setting: str, value: float, per_day: float) -> float:
    """`value` of the setting `setting` (a duration in a unit of which
    `per_day` make a day), or a century in that unit when it is longer,
    which the log says once per setting and process. Taken where the
    duration is added to a moment or taken from it, never at the start: every
    value an install starts with starts, with the meaning it has (0 or less
    means what each use made of it before; decided on 8 Nov 2026).
    A value no clock holds made a moment out of range where it was used:
    AUDIT_RETENTION_DAYS=800000 stopped the purge every hour, SESSION_HOURS
    of a hundred million every sign-in (the review of piece 1 of agent
    notices, 8 Nov 2026, found it on READ_MARKS_RETENTION_DAYS)."""
    most = CENTURY_DAYS * per_day
    if value <= most:
        return value
    if setting not in _SAID:
        _SAID.add(setting)
        logger.warning("%s=%s is longer than a century: read as a century, which this server "
                       "never reaches", setting.upper(), value)
    return most
