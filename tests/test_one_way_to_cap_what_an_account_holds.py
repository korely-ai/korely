"""The most of a kind of row an account may hold is counted one way
(services/caps): the account's lock of that kind to the commit, the count,
the rollback and the refusal. Four copies of those steps (keys, projects,
webhook endpoints, declared names) had begun to differ (the audit of 27 Oct
2026); each cap's own refusal is in its own tests (test_account_keys_bounded,
test_projects_are_capped, test_webhook_schedule, test_names_capped)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import text

from korely_agent.services import caps

PACKAGE = Path(__file__).resolve().parents[1] / "korely_agent"
THE_CAPS = {"services/keys.py": "api-keys", "services/projects.py": "projects",
            "services/webhooks.py": "webhook-endpoints",
            "services/facts/entities.py": "entity-aliases"}


class Full(ValueError):
    pass


def _locks_here(db) -> int:
    return db.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                           " AND pid = pg_backend_pid()")).scalar()


def test_one_more_goes_in_below_the_cap_under_the_accounts_lock(db, tenant):
    assert caps.room_for_one_more(db, "things", tenant.id, lambda: 2, 3, Full) == 2
    assert _locks_here(db) == 1, "the lock is held to the commit"
    db.commit()
    assert _locks_here(db) == 0


def test_at_the_cap_the_write_is_rolled_back_and_refused(db, tenant):
    db.execute(text("CREATE TEMP TABLE t (x int)"))
    db.execute(text("INSERT INTO t VALUES (1)"))
    with pytest.raises(Full, match="^3$"):
        caps.room_for_one_more(db, "things", tenant.id, lambda: 3, 3, lambda n: Full(str(n)))
    assert _locks_here(db) == 0
    assert db.execute(text("SELECT to_regclass('pg_temp.t')")).scalar() is None, \
        "the transaction was rolled back"


def test_a_write_that_adds_nothing_is_never_refused(db, tenant):
    assert caps.room_for_one_more(db, "things", tenant.id, lambda: None, 0, Full) is None


@pytest.mark.parametrize("rel, kind", sorted(THE_CAPS.items()))
def test_every_cap_counts_through_the_one_place(rel, kind):
    """Each cap calls services/caps with its kind, and takes no lock of its
    kind by hand."""
    source = (PACKAGE / rel).read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "room_for_one_more"]
    assert [ast.literal_eval(c.args[1]) for c in calls] == [kind]
    assert f'"{kind}:' not in source and f"f\"{kind}:" not in source
