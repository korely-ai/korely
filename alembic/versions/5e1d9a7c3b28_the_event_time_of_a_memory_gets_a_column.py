"""the event time of a memory gets a column of its own

Revision ID: 5e1d9a7c3b28
Revises: c9c31d7d46d7
Create Date: 2026-09-28

A write on /v1 carries `timestamp`, the moment the events it tells happened:
the facts mined from it inherit it as `valid_from`, and that is what makes
`as_of` answer "what was true then" instead of "what was written then"
(Graphiti, valid time against transaction time).

Until now that moment had no place of its own:

  * the asynchronous write and every /v1/batch item put it INSIDE the
    developer's `metadata`, as `_event_time`, and from there it came back on
    every read (GET /v1/memories, search, the detail): a key of ours in
    somebody else's data, and a `_event_time` the developer wrote themselves
    decided the dates of the facts;
  * the synchronous write (WRITE_ASYNC=false) did not store it at all: when
    its inline extraction failed, the worker finished the memory later with
    "now", and the time the caller asked for was lost.

The column holds it for every write, and the worker reads only the column.

Existing rows. Those with `_event_time` in the metadata move it into the
column, read with a copy of the code's parser (`services.facts.parse_event_time`:
ISO date or datetime, no offset = UTC), and the key leaves the metadata.
Metadata left empty goes back to NULL, as it was before the key was put
there. An unreadable value leaves the column NULL, which is what the worker
already made of it ("now"), and leaves the metadata too: that key was never
the developer's.

`metadata` is `json`, not `jsonb`, and the rows are selected by a text search
and rewritten from Python rather than cast: a `json` value can hold a `\\u0000`
escape that `jsonb` refuses, and one such row would stop the upgrade, and with
it the container, which runs `alembic upgrade` before it serves.

The downgrade puts `_event_time` back in the metadata of every row that has an
event time, written in UTC as the old code wrote it (`isoformat()`), since that
is the only place the previous version looks for it; then drops the column. A
row written before this migration comes back as it was, the instant intact (an
offset other than UTC is written in UTC).
"""
import json
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from alembic import op

revision = "5e1d9a7c3b28"
down_revision = "c9c31d7d46d7"
branch_labels = None
depends_on = None

_KEY = "_event_time"


def _parse(s) -> Optional[datetime]:
    """A copy of `services.facts.parse_event_time` as of 2026-09-28. A
    migration does not import the application, which tomorrow may differ."""
    if not s or not isinstance(s, str):
        return None
    raw = s.strip().replace("Z", "+00:00")
    dt = None
    for candidate in (raw, raw + "T00:00:00"):
        try:
            dt = datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def upgrade() -> None:
    op.add_column("agent_memories",
                  sa.Column("event_time", sa.DateTime(timezone=True), nullable=True))
    bind = op.get_bind()
    # A superset (any row whose text holds the key's name); Python decides.
    rows = bind.execute(sa.text(
        "SELECT id, metadata::text FROM agent_memories "
        "WHERE strpos(metadata::text, :needle) > 0"
    ), {"needle": f'"{_KEY}"'}).fetchall()
    for id_, text in rows:
        meta = json.loads(text)
        if not isinstance(meta, dict) or _KEY not in meta:
            continue
        ev = _parse(meta.pop(_KEY))
        bind.execute(
            sa.text("UPDATE agent_memories SET event_time = :ev, "
                    "metadata = CAST(:meta AS json) WHERE id = :id"),
            {"ev": ev, "meta": json.dumps(meta) if meta else None, "id": id_},
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, event_time, metadata::text FROM agent_memories "
        "WHERE event_time IS NOT NULL"
    )).fetchall()
    for id_, ev, text in rows:
        meta = json.loads(text) if text is not None else None
        if not isinstance(meta, dict):
            meta = {}
        meta[_KEY] = ev.astimezone(timezone.utc).isoformat()
        bind.execute(
            sa.text("UPDATE agent_memories SET metadata = CAST(:meta AS json) WHERE id = :id"),
            {"meta": json.dumps(meta), "id": id_},
        )
    op.drop_column("agent_memories", "event_time")
