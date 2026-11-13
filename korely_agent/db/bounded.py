"""Reads the one API process can afford (audit of 20 Oct 2026).

The API is one process, with the worker inside it, under one memory limit
(docker-compose.yml, API_MEMORY_LIMIT), serving every account of the install.
A read that brings a whole table, a whole account or a whole end user into it
at once grows with somebody's data, and past the limit the process is killed
for everybody. The audit found nine of them: the Markdown export, a project's
deletion, two dashboard reports, three listings paged in Python, a batch
job's status and claim, each loading what the database could have counted,
cut or left where it was.

The rule every read of a large table follows, here and in the models:

- what is shown a page at a time is cut by the database (LIMIT and OFFSET,
  or a key), and its total is a COUNT, never `len()` of everything;
- what is counted or grouped is counted or grouped by the database (GROUP BY,
  COUNT, COUNT(DISTINCT)), never in a Python loop over the rows;
- what must walk every row (an export) walks it `STREAM_ROWS` at a time
  (`stream`), the columns it reads and nothing else;
- what is locked is locked by the database without coming back (`lock_rows`);
- a vector or a stored payload is read only by the read that needs it: the
  models defer those columns (db/models.py, `deferred`), so loading a row
  never brings them.

tests/test_no_read_loads_a_whole_table.py holds the code to it: every whole
read (`.all()`, a loop over a query) of a large table carries a LIMIT or a
`yield_per`, or is named there with the reason it is bounded.
"""
from __future__ import annotations

from sqlalchemy import func, select

#: How many rows a streamed read fetches per round trip (`stream`). A row of
#: the text columns an export or a scan reads is well under a kilobyte, so a
#: thousand of them is about a megabyte held at once, and a table of a
#: million rows is a thousand round trips: the memory stays flat and the time
#: is the database's.
STREAM_ROWS = 1000


def stream(query, rows: int | None = None):
    """`query`'s rows `rows` (STREAM_ROWS) at a time, from a server-side cursor
    (`Query.yield_per`): what has been read can go before the next batch
    comes. Rows of columns, never entities a loop keeps: the session's
    identity map would keep them all anyway."""
    return query.yield_per(rows or STREAM_ROWS)


def lock_rows(db, query) -> int:
    """Lock the rows `query` names (FOR UPDATE, in its order) without bringing
    them back, and return how many: `SELECT count(*) FROM (... FOR UPDATE)`.
    `query.with_for_update().all()` locked the same rows and returned each
    one's id, every memory of an end user or a project in one list."""
    locked = query.with_for_update().subquery()
    return int(db.execute(select(func.count()).select_from(locked)).scalar() or 0)
