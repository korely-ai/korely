"""the trail's lists of confined end users, whole entries only

Revision ID: c45a7e2b9d13
Revises: a4c8e2f6b9d1
Create Date: 2026-10-18 17:00:00

A key or an OAuth consent confined to end users records its list in the
trail (`audit_events.meta.users`), and the trail kept any list as its text
cut at 200 characters: past five or six email-shaped ids, one id was cut in
the middle. The erasure rewrites an erased id there by the whole entry
(korely_agent/services/pseudonyms.py), so a cut one kept a prefix of the
erased person's id for the trail's 400 days (audit of 18 Oct 2026). The
trail now keeps whole entries and how many were left out
(services/audit._whole_entries). This does the same, once, for the rows
written before: a list that does not end where a list ends keeps the entries
before the cut, and says that more were left out. Nobody can tell how many.

The downgrade changes nothing back: the cut fragment is not stored anywhere.
"""
import ast
import json

import sqlalchemy as sa
from alembic import op

revision = "c45a7e2b9d13"
down_revision = "a4c8e2f6b9d1"
branch_labels = None
depends_on = None


def _whole(cut: str) -> str:
    """The entries of a cut list's text that are whole, as a list's text, and
    a mark that some were left out."""
    end = len(cut)
    while True:
        end = cut.rfind(", ", 0, end)
        if end < 0:
            return "[] (+more)"
        try:
            kept = ast.literal_eval(cut[:end] + "]")
        except (SyntaxError, ValueError):
            continue
        if isinstance(kept, list) and all(isinstance(x, str) for x in kept):
            return str(kept) + " (+more)"


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, meta FROM audit_events "
        "WHERE meta IS NOT NULL AND (meta->>'users') IS NOT NULL "
        "AND (meta->>'users') LIKE '[%' AND right(meta->>'users', 1) <> ']' "
        "AND (meta->>'users') NOT LIKE '%more)'")).fetchall()
    for row_id, meta in rows:
        meta = dict(meta if isinstance(meta, dict) else json.loads(meta))
        meta["users"] = _whole(str(meta["users"]))
        bind.execute(sa.text("UPDATE audit_events SET meta = CAST(:m AS json) WHERE id = :id"),
                     {"m": json.dumps(meta), "id": row_id})


def downgrade() -> None:
    pass
