"""The migration of the windows' table (e5c9a2f7d3b1) waits for its lock on
`agent_memories` at most LOCK_TIMEOUT an attempt, tries again, and keeps the
timeout to itself (alembic/env.py runs every pending migration in one
transaction): read on 17 Nov 2026, a timeout of the whole run stopped the
image's start (`set -e`) at the first long transaction on the memories, and
held for every migration after it.
"""
import importlib.util
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError

PATH = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
        / "e5c9a2f7d3b1_a_long_memory_has_a_vector_for_each_window.py")


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("windows_migration", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "LOCK_TIMEOUT", "200ms")
    return mod


def _the_migrations_lock(conn):
    return lambda: conn.exec_driver_sql("LOCK TABLE agent_memories IN SHARE ROW EXCLUSIVE MODE")


def _held_for(engine, seconds):
    """A writer's lock on agent_memories, held `seconds` in a thread (None:
    until the event the caller sets)."""
    taken, done = threading.Event(), threading.Event()

    def hold():
        with engine.connect() as c:
            c.exec_driver_sql("LOCK TABLE agent_memories IN ROW EXCLUSIVE MODE")
            taken.set()
            done.wait(seconds if seconds is not None else 30)
            c.rollback()

    t = threading.Thread(target=hold)
    t.start()
    taken.wait(10)
    return t, done


def test_the_timeout_is_the_migrations_only(engine, migration):
    """The transaction's own lock_timeout comes back after it, whatever it
    was (7 s here, not the server's default)."""
    with engine.connect() as conn:
        with conn.begin():
            conn.exec_driver_sql("SET LOCAL lock_timeout = '7s'")
            migration.with_the_lock(conn, lambda: conn.exec_driver_sql("SELECT 1"))
            assert conn.exec_driver_sql("SHOW lock_timeout").scalar() == "7s"


def test_the_attempts_outlast_a_writer_back_to_back(engine, migration):
    """With the migration's own number of attempts: a writer that holds the
    table for several timeouts is outlasted, and the attempts follow each
    other with no pause (no idle time with an earlier migration's locks
    held)."""
    t, done = _held_for(engine, 0.7)
    tries = []
    with engine.connect() as conn:
        with conn.begin():
            def do():
                tries.append(time.monotonic())
                _the_migrations_lock(conn)()

            migration.with_the_lock(conn, do)
    done.set()
    t.join(10)
    assert len(tries) >= 3, tries
    gaps = [b - a for a, b in zip(tries, tries[1:])]
    assert max(gaps) < 0.6, gaps     # 200 ms of lock_timeout, and nothing more


def test_an_attempt_that_times_out_is_tried_again(engine, migration, monkeypatch):
    monkeypatch.setattr(migration, "ATTEMPTS", 6)
    t, done = _held_for(engine, 0.6)
    tries = []
    with engine.connect() as conn:
        with conn.begin():
            def do():
                tries.append(time.monotonic())
                _the_migrations_lock(conn)()

            migration.with_the_lock(conn, do)
            assert conn.exec_driver_sql("SHOW lock_timeout").scalar() == "0"
    done.set()
    t.join(10)
    assert len(tries) >= 2, "it never waited for the writer's lock"


def test_when_every_attempt_times_out_the_run_fails(engine, migration, monkeypatch):
    monkeypatch.setattr(migration, "ATTEMPTS", 2)
    t, done = _held_for(engine, None)
    try:
        with engine.connect() as conn:
            with pytest.raises(OperationalError) as e:
                with conn.begin():
                    migration.with_the_lock(conn, _the_migrations_lock(conn))
            assert e.value.orig.pgcode == "55P03"
    finally:
        done.set()
        t.join(10)


def test_another_error_is_not_tried_again(engine, migration):
    tries = []
    with engine.connect() as conn:
        with pytest.raises(ProgrammingError):
            with conn.begin():
                def do():
                    tries.append(1)
                    conn.exec_driver_sql("SELECT * FROM no_such_table_here")

                migration.with_the_lock(conn, do)
    assert tries == [1]


def test_another_operational_error_is_not_tried_again(engine, migration):
    """A statement cancelled for its own time (57014) is no lock not given:
    raised at the first attempt."""
    tries = []
    with engine.connect() as conn:
        with pytest.raises(OperationalError) as e:
            with conn.begin():
                def do():
                    tries.append(1)
                    conn.exec_driver_sql("SET LOCAL statement_timeout = '50ms'")
                    conn.exec_driver_sql("SELECT pg_sleep(2)")

                migration.with_the_lock(conn, do)
    assert e.value.orig.pgcode == "57014" and tries == [1]


def test_a_deadlocks_victim_is_tried_again(engine, migration):
    """Chosen as a deadlock's victim (40P01), the attempt's savepoint is
    rolled back and it is tried again."""
    class Victim(Exception):
        pgcode = "40P01"

    tries = []
    with engine.connect() as conn:
        with conn.begin():
            def do():
                tries.append(1)
                if len(tries) == 1:
                    raise OperationalError("LOCK TABLE agent_memories", {}, Victim())

            migration.with_the_lock(conn, do)
    assert tries == [1, 1]


def test_the_upgrade_and_the_downgrade_go_through_the_lock(migration, monkeypatch):
    from types import SimpleNamespace

    done = []
    monkeypatch.setattr(migration, "op", SimpleNamespace(
        get_bind=lambda: "the bind", drop_table=lambda name: done.append(("drop", name))))
    monkeypatch.setattr(migration, "with_the_lock",
                        lambda conn, do: done.append(("lock", conn, do)) or do())
    monkeypatch.setattr(migration, "_create", lambda: done.append(("create",)))
    migration.upgrade()
    migration.downgrade()
    assert done == [("lock", "the bind", migration._create), ("create",),
                    ("lock", "the bind", done[2][2]), ("drop", "agent_memory_windows")]


def test_the_migration_is_read_without_a_database():
    """`alembic upgrade d8a4f2c6e1b9:e5c9a2f7d3b1 --sql`, the dry reading of
    the manual of a deploy, stopped at the migration's SHOW and savepoint,
    which need a connection (18 Nov 2026): it writes the one attempt's SQL,
    the lock wait bounded and then given back."""
    import subprocess
    import sys

    root = PATH.parents[2]
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", "upgrade",
         "d8a4f2c6e1b9:e5c9a2f7d3b1", "--sql"],
        cwd=root, capture_output=True, text=True, timeout=120,
        env={**__import__("os").environ, "DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none"})
    assert r.returncode == 0, r.stderr[-2000:]
    sql = r.stdout
    bounded = sql.index("SET LOCAL lock_timeout = '")
    made = sql.index("CREATE TABLE agent_memory_windows")
    back = sql.index("SET LOCAL lock_timeout TO DEFAULT")
    assert bounded < made < back
    assert "SHOW lock_timeout" not in sql and "SAVEPOINT" not in sql
