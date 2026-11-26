"""The nearest facts by meaning, on the HNSW indexes (memory_core 2.12.0).

BUG-320 and the code audit of 6 Oct. The facts' vector indexes
(`agent_facts.embedding`) are HNSW indexes over every account's
rows, and the filters of a memory (account, project, end user) apply after
the index is scanned. pgvector's README (read on 2026-10-06, the dev database
runs 0.8.0, production 0.8.2): "Specify the size of the dynamic candidate
list for search (40 by default)"; under Filtering, "With approximate
indexes, filtering is applied *after* the index is scanned. If a condition
matches 10% of rows, with HNSW and the default `hnsw.ef_search` of 40, only
4 rows will match on average. For more rows, enable iterative index scans,
which will automatically scan more of the index when needed"; under
Iterative Index Scans, "Starting with 0.8.0, you can enable iterative index
scans, which will automatically scan more of the index until enough results
are found (or it reaches `hnsw.max_scan_tuples` ...)" and "Strict ensures
results are in the exact order by distance"; and "Use `SET LOCAL` inside a
transaction to set it for a single query".

So the judge's semantic candidates (`candidates.for_the_judge`, stage 1b),
the later facts near a late vector (`judge.after_the_vector`) and the
restatements of a write (`twins.find_twin`) asked for 10 or 20 rows and
could get fewer, or none, while the memory held the fact a contradiction
written with other words needed. `/v1/context` set the depth since
BUG-298 (the same rule, which could not be imported from here: memory_core
imports nothing from the application); on a throwaway pgvector 0.8.0 database,
10,100 rows whose filter kept 100, LIMIT 10 returned 1 row by default and 10
with the setting, in the exact order of a sequential scan (BUG-320).

`nearest` is the one way the engine asks for the nearest rows: the depth,
then the query, on PostgreSQL; elsewhere (SQLite, the test suites) the
distances in Python with `cosine`, so the code that uses the result runs in
the tests too.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from . import log_safe as _log
from .store import _pg

logger = logging.getLogger(__name__)

# The README's default, and the maximum of the setting (pg_settings.max_val,
# pgvector 0.8.0).
EF_SEARCH_DEFAULT = 40
EF_SEARCH_MAX = 1000

_PGVECTOR_VERSION: Dict[str, Tuple[int, ...]] = {}


def _pgvector_version(db) -> Tuple[int, ...]:
    """The pgvector installed in the database, as (major, minor, patch); read
    once per database URL and process (it changes only with a deploy)."""
    bind = db.get_bind()
    key = str(getattr(bind, "engine", bind).url)
    if key not in _PGVECTOR_VERSION:
        version = db.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar()
        _PGVECTOR_VERSION[key] = tuple(int(p) for p in re.findall(r"\d+", version or "")[:3])
    return _PGVECTOR_VERSION[key]


def hnsw_depth(db, pool: int) -> None:
    """Let the HNSW index scans of this transaction bring `pool` rows that
    pass the filters: `SET LOCAL hnsw.ef_search` to the pool (at least the
    default 40, at most 1000) and, from pgvector 0.8.0, `SET LOCAL
    hnsw.iterative_scan = strict_order`. The setting ends with the
    transaction; a later HNSW query in it only scans deeper. Nothing off
    PostgreSQL. Never raises: the SETs run in a savepoint of their own (inside
    the caller's, when there is one), and a failure leaves the defaults, the
    query as it was before."""
    if not _pg(db):
        return
    ef = min(max(EF_SEARCH_DEFAULT, int(pool)), EF_SEARCH_MAX)
    try:
        with db.begin_nested():
            db.execute(text(f"SET LOCAL hnsw.ef_search = {ef}"))
            if _pgvector_version(db) >= (0, 8, 0):
                db.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    except Exception as e:  # noqa: BLE001 - a setting never fails the write
        logger.warning("[memory] hnsw.ef_search not set (%s): %s", ef, _log.error(e))


def cosine(a, b) -> Optional[float]:
    """Cosine distance between two vectors, or None when it cannot be computed.
    In Python: the candidates are already loaded, a round trip to have Postgres
    compute a handful of distances costs more than the arithmetic.

    None for a vector that is missing or not a sequence of numbers (TypeError)
    or not usable as one (ValueError), and for a zero vector. Anything else is
    a defect and propagates (2.11.0): `except Exception` hid one as "too far to
    be a twin" (an audit of memory_core 2.10.0)."""
    try:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        if not na or not nb:
            return None
        return 1.0 - dot / (na * nb)
    except (TypeError, ValueError):
        return None


def nearest(db, q, column, vector, limit: int) -> List[Tuple[Any, Optional[float]]]:
    """The rows of query `q` nearest to `vector` by cosine distance on
    `column` (a vector column of the rows' model), nearest first, at most
    `limit`, each with its distance: [(row, distance)].

    On PostgreSQL the database orders them (on the HNSW index when the
    planner takes it), with `hnsw_depth(db, limit)` first. Elsewhere the
    distances are computed here, rows with none last. `q` holds the caller's
    filters, the memory's wall among them."""
    limit = max(1, int(limit))
    if _pg(db):
        hnsw_depth(db, limit)
        dist = column.cosine_distance(vector).label("d")
        return [
            (r, float(d) if d is not None else None)
            for r, d in q.add_columns(dist).order_by(dist).limit(limit).all()
        ]
    name = column.key
    pairs = [(r, cosine(vector, getattr(r, name))) for r in q.all()]
    pairs.sort(key=lambda rd: 2.0 if rd[1] is None else rd[1])
    return pairs[:limit]
