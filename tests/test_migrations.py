"""The migrations, run for real, on a database of their own.

The rest of the suite builds its schema from the models (`create_all`), so it
never runs a migration: the codebase this came from shipped a migration that
referenced a column which did not exist, passed every test that way, and went
down in a boot loop in production. `make migrate-check` applies them, and
`scripts/release.sh` runs `make test-all`, not that. So the part a release has
to vouch for is here too:

- head is the models: the tables and columns the migrations build are the ones
  the code declares;
- every downgrade gives back exactly the schema of the revision before it
  (columns, types, defaults, indexes, constraints), which is what the README's
  way back relies on;
- an install that holds rows in a table a migration drops still upgrades
  (`agent_shadow_accounts`, the hosted service's signup table, c9c31d7d46d7).

Each test gets a scratch database next to the test one and drops it after, so
nothing here touches the tables the other tests use. alembic runs in a
subprocess, as the entrypoint runs it: its env.py configures logging and reads
the settings, and neither should leak into this process.
"""
import errno
import os
import subprocess
import sys
import uuid
import warnings
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from korely_agent.db import Base
from tests.conftest import TEST_DB, server_url


ROOT = Path(__file__).resolve().parents[1]


def _chain() -> list[str]:
    """Every revision, oldest first. Read from the files; no database.
    alembic.ini's `script_location` is relative to the working directory
    (the image puts the ini at /app, beside /app/alembic, so `%(here)s`
    would not hold there), and a run started elsewhere found no `alembic`
    (the audit of 31 Oct 2026): the folder is given from this file, as in
    test_hosted_mode."""
    cfg = Config(str(ROOT / "alembic" / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1, f"more than one head: {script.get_heads()}"
    return [r.revision for r in reversed(list(script.walk_revisions()))]


def _on_the_server(statement: str) -> None:
    admin = create_engine(server_url(), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(statement))
    finally:
        admin.dispose()


def _new_database(url: str, eng) -> None:
    """The database `url` names, as an install starts from: new, with
    pgvector and nothing else. One that is there is dropped first, the
    engine's connections closed before."""
    name = make_url(url).database
    eng.dispose()
    _on_the_server(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    _on_the_server(f'CREATE DATABASE "{name}"')
    with eng.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))


@pytest.fixture
def scratch():
    """(url, engine) of an empty database with pgvector, dropped afterwards."""
    name = f"korely_migrations_{uuid.uuid4().hex[:8]}"
    url = make_url(TEST_DB).set(database=name).render_as_string(hide_password=False)
    eng = create_engine(url)
    try:
        _new_database(url, eng)
        yield url, eng
    finally:
        eng.dispose()
        _on_the_server(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# ── alembic in processes of its own, as the entrypoint runs it ─────────────
# On a Mac a checkout mounted in the test container reaches it through Docker
# Desktop's file sharing, and there a process whose working directory is the
# checkout can find os.getcwd() failing for a moment, a FileNotFoundError
# that names no file, while any container on the machine starts or stops.
# Measured on 8 Nov 2026 with a probe in the suite's image (os.getcwd() every
# millisecond, and in a new process every 50 ms): 67 failures in 17 of 21
# phases of 8 to 10 s of starting other containers (with this folder mounted,
# one inside it, its parent, a sibling, an unrelated one, or none), and 3
# within a second after one; windows of 1 to 210 ms (median 36). None in 48 s
# with nothing starting, none in 95 s of writes, renames and touches from the
# Mac, and in 100 s of writes as Python writes bytecode, from a running
# container, 3, each within a second of another container starting or
# stopping. alembic's process calls it five times as it starts (for
# alembic.ini and pyproject.toml, for env.py, at its first import after the
# ini's `prepend_sys_path = .` adds the working directory to its path, and
# for the versions' folder), all before its first migration; this file starts
# some 170 of them, 133 in
# test_every_downgrade_gives_back_the_schema_before_it, which fell so in a
# gate of 8 Nov 2026 while other containers started on the machine. The cause
# is outside this code and outside the product (the image runs alembic from
# its own files), and the Makefile removes it: `make test`, `make test-all`
# and `make migrate-check` run from a copy of the checkout made inside the
# container (scripts/from_a_copy.sh), where no working directory is in the
# shared folder. Until runs from the copy have shown that alone (beside a
# container started every second, one whole run of this file and part of a
# second with no loss, 8 Nov 2026), and for a run by hand from the mounted
# checkout, a process that died of it is run once more, with a warning in the
# run's summary that says so; a second loss, or any other failure, fails as
# before.
_CWD_LOST = f"{FileNotFoundError.__name__}: {FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))}"


def _lost_its_working_directory(run: subprocess.CompletedProcess) -> bool:
    """Whether the process died of os.getcwd() failing: the last line of its
    traceback is a FileNotFoundError that names no file (one that could not
    open a file names it)."""
    return run.returncode != 0 and run.stderr.rstrip().rpartition("\n")[2] == _CWD_LOST


def _alembic_at_once(url: str, *commands: tuple[str, ...]) -> list[subprocess.CompletedProcess]:
    """Each command (alembic's arguments) in a process of its own, all started
    at once; what each answered."""
    env = {**os.environ, "DATABASE_URL": url}
    started = [subprocess.Popen([sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", *args],
                                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
               for args in commands]
    answered = []
    for p in started:
        out, err = p.communicate(timeout=300)
        answered.append(subprocess.CompletedProcess(p.args, p.returncode, out, err))
    return answered


def _alembic_runs(url: str, *commands: tuple[str, ...],
                  start_again=None) -> list[subprocess.CompletedProcess]:
    """`_alembic_at_once`, all of it once more if a process lost its working
    directory (above); `start_again` first puts back the database the
    commands started from, when one of them may have changed it."""
    runs = _alembic_at_once(url, *commands)
    lost = [" ".join(r.args[5:]) for r in runs if _lost_its_working_directory(r)]
    if not lost:
        return runs
    warnings.warn(f"alembic {' and '.join(lost)}: os.getcwd() failed in its process (a moment of "
                  "Docker Desktop's file sharing, see tests/test_migrations.py); run once more",
                  stacklevel=2)
    if start_again is not None:
        start_again()
    return _alembic_at_once(url, *commands)


def _alembic(url: str, *args: str) -> None:
    (r,) = _alembic_runs(url, args)
    assert r.returncode == 0, f"alembic {' '.join(args)} failed:\n{r.stderr[-3000:]}"


def _a_process_that_lost_its_working_directory(tmp_path) -> subprocess.CompletedProcess:
    """A Python process whose working directory goes away under it and which
    then imports through a relative entry of its path, as alembic's does
    after `prepend_sys_path = .`: what it answered, with alembic's arguments."""
    gone = tmp_path / "gone"
    gone.mkdir()
    r = subprocess.run([sys.executable, "-I", "-c",
                        "import os, sys; os.rmdir(os.getcwd()); sys.path.insert(0, '.'); import fractions"],
                       cwd=gone, capture_output=True, text=True)
    return subprocess.CompletedProcess(
        [sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", "upgrade", "head"],
        r.returncode, r.stdout, r.stderr)


def test_a_lost_working_directory_is_told_from_any_other_failure(tmp_path):
    """What `_lost_its_working_directory` reads is what such a process says,
    through the frames of the gate's traceback (8 Nov 2026); a process that
    cannot open a file, or fails or ends on its own, is not one."""
    lost = _a_process_that_lost_its_working_directory(tmp_path)
    assert "path_hook_for_FileFinder" in lost.stderr, lost.stderr
    assert _lost_its_working_directory(lost)
    for code in ("open('missing')", "import os; os.listdir('missing')", "raise SystemExit(1)",
                 "raise FileNotFoundError()", "import os; os.getcwd()"):
        r = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, capture_output=True, text=True)
        assert not _lost_its_working_directory(r), (code, r.stderr)


def test_alembic_runs_once_more_only_after_a_lost_working_directory(tmp_path, monkeypatch):
    """One more try, for that failure only, said in a warning; a second loss
    or any other failure is answered as it came, and the database is put
    back before the second try when the caller says how."""
    lost = _a_process_that_lost_its_working_directory(tmp_path)
    ok = subprocess.CompletedProcess(lost.args, 0, "", "")
    other = subprocess.CompletedProcess(lost.args, 1, "", "Traceback (most recent call last):\n"
                                        "sqlalchemy.exc.ProgrammingError: relation does not exist")
    for answers, want, tries in (([[ok]], [ok], 1),
                                 ([[other]], [other], 1),
                                 ([[lost], [ok]], [ok], 2),
                                 ([[lost], [lost]], [lost], 2),
                                 ([[lost], [other]], [other], 2),
                                 ([[ok, lost], [ok, ok]], [ok, ok], 2)):
        queue, put_back = list(answers), []

        def at_once(url, *commands):
            assert len(commands) == len(queue[0])
            return queue.pop(0)

        monkeypatch.setitem(globals(), "_alembic_at_once", at_once)
        with warnings.catch_warnings(record=True) as said:
            warnings.simplefilter("always")
            got = _alembic_runs("postgresql://x", *([("upgrade", "head")] * len(answers[0])),
                                start_again=lambda: put_back.append(True))
        assert got == want and len(answers) - len(queue) == tries
        assert put_back == [True] * (tries - 1)
        assert [str(w.message) for w in said] == ([] if tries == 1 else [
            "alembic upgrade head: os.getcwd() failed in its process (a moment of Docker Desktop's "
            "file sharing, see tests/test_migrations.py); run once more"])


def _schema(eng) -> dict:
    """Everything a downgrade has to give back, as sets that compare."""
    with eng.connect() as c:
        return {
            "columns": set(c.execute(text(
                "SELECT table_name, column_name, data_type, character_maximum_length,"
                " is_nullable, column_default FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name <> 'alembic_version'"))),
            "indexes": set(c.execute(text(
                "SELECT tablename, indexname, indexdef FROM pg_indexes"
                " WHERE schemaname = 'public' AND tablename <> 'alembic_version'"))),
            "constraints": set(c.execute(text(
                "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)"
                " FROM pg_constraint WHERE connamespace = 'public'::regnamespace"
                " AND conrelid::regclass::text <> 'alembic_version'"))),
        }


def _tables(eng) -> set[str]:
    return {t for t in inspect(eng).get_table_names() if t != "alembic_version"}


def test_head_is_the_models(scratch):
    url, eng = scratch
    _alembic(url, "upgrade", "head")
    from korely_agent.core import extensions

    # A tree without the Cloud's package has the Cloud's tables from the one
    # chain and not their models (alembic/env.py leaves them out the same way).
    have = _tables(eng) - extensions.cloud_tables_without_models(_tables(eng))
    want = set(Base.metadata.tables)
    assert have == want, (f"only the migrations create: {sorted(have - want)}; "
                          f"only the models declare: {sorted(want - have)}")
    drift = {}
    for t in sorted(have):
        in_db = {c["name"] for c in inspect(eng).get_columns(t)}
        in_models = set(Base.metadata.tables[t].c.keys())
        if in_db != in_models:
            drift[t] = {"only in the database": sorted(in_db - in_models),
                        "only in the models": sorted(in_models - in_db)}
    assert drift == {}, drift


def test_autogenerate_finds_nothing_to_write(scratch):
    """`alembic check` compares the models with a database at head the way
    `alembic revision --autogenerate` would: tables, columns, types,
    nullability, indexes, foreign keys. The test above sees tables and columns
    only, and so did `make migrate-check`, which is how a migration came to
    create `idx_agent_batch_project` while the model never declared it: the
    next autogenerate would have written a migration dropping it."""
    url, _eng = scratch
    _alembic(url, "upgrade", "head")
    (r,) = _alembic_runs(url, ("check",))
    assert r.returncode == 0, (
        "the models and the migrations disagree; the side that is wrong is the "
        "one to change (the model, or a new migration):\n" + (r.stdout + r.stderr)[-3000:])


def test_every_downgrade_gives_back_the_schema_before_it(scratch):
    url, eng = scratch
    chain = _chain()
    before = {}
    for rev in chain:
        _alembic(url, "upgrade", rev)
        before[rev] = _schema(eng)
    wrong = []
    for newer, older in zip(reversed(chain), reversed(chain[:-1])):
        _alembic(url, "downgrade", older)
        now = _schema(eng)
        for part, rows in before[older].items():
            if now[part] != rows:
                wrong.append(f"downgrading {newer} to {older}, the {part} differ:\n"
                             f"  extra   {sorted(now[part] - rows)[:5]}\n"
                             f"  missing {sorted(rows - now[part])[:5]}")
    assert wrong == [], "\n".join(wrong)


def test_an_install_with_hosted_signup_rows_upgrades(scratch):
    """No version of this product wrote to agent_shadow_accounts; an install
    has rows there only if somebody put them there. The upgrade must not stop
    on them, and must leave everything else alone."""
    url, eng = scratch
    _alembic(url, "upgrade", "a3c5e7f9b204")
    tenant = uuid.uuid4()
    with eng.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"), {"id": tenant})
        c.execute(text("INSERT INTO agent_shadow_accounts (id, tenant_id, agent_caller, signup_ip)"
                       " VALUES (:id, :t, 'claude-code', '203.0.113.7')"),
                  {"id": uuid.uuid4(), "t": tenant})

    _alembic(url, "upgrade", "head")
    assert "agent_shadow_accounts" not in _tables(eng)
    with eng.connect() as c:
        assert c.execute(text("SELECT name FROM tenants WHERE id = :id"),
                         {"id": tenant}).scalar_one() == "Acme"

    _alembic(url, "downgrade", "a3c5e7f9b204")
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM agent_shadow_accounts")).scalar_one() == 0, (
            "the downgrade recreates the table empty: what the upgrade deleted is in the backup")


# ── 5e1d9a7c3b28: the event time moves out of the developer's metadata ─────

_BEFORE_EVENT_TIME = "c9c31d7d46d7"
_EVENT_TIME = "5e1d9a7c3b28"


def _memory(c, tenant, meta):
    import json

    mid = uuid.uuid4()
    c.execute(text("INSERT INTO agent_memories (id, tenant_id, content, metadata)"
                   " VALUES (:id, :t, 'x', CAST(:m AS json))"),
              {"id": mid, "t": tenant, "m": None if meta is None else json.dumps(meta)})
    return mid


def _read(eng, mid):
    import json

    with eng.connect() as c:
        ev, meta = c.execute(text("SELECT event_time, metadata::text FROM agent_memories"
                                  " WHERE id = :id"), {"id": mid}).one()
    return ev, (None if meta is None else json.loads(meta))


def test_the_event_time_moves_into_its_column_and_back(scratch):
    """What the asynchronous write and the batch import stashed as
    `_event_time` in the developer's metadata becomes the column, parsed the
    way the code parses it; the key leaves the metadata, and metadata left
    empty is NULL again, as it was before the key was put there. Going back
    puts it where the old code reads it."""
    from datetime import datetime, timezone

    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE_EVENT_TIME)
    tenant = uuid.uuid4()
    with eng.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"), {"id": tenant})
        only = _memory(c, tenant, {"_event_time": "2026-03-01T00:00:00+00:00"})
        mixed = _memory(c, tenant, {"_event_time": "2026-03-01T14:30:00+01:00", "tier": "pro"})
        unreadable = _memory(c, tenant, {"_event_time": "yesterday", "tier": "x"})
        theirs = _memory(c, tenant, {"tier": "y"})
        nothing = _memory(c, tenant, None)
        # A NUL escape is valid json and not valid jsonb: the migration must
        # not cast every row to jsonb, or one such row stops the upgrade and
        # the container with it.
        nul = _memory(c, tenant, {"_event_time": "2026-03-02", "note": "a\u0000b"})

    _alembic(url, "upgrade", _EVENT_TIME)
    utc = timezone.utc
    assert _read(eng, only) == (datetime(2026, 3, 1, tzinfo=utc), None)
    assert _read(eng, mixed) == (datetime(2026, 3, 1, 13, 30, tzinfo=utc), {"tier": "pro"})
    assert _read(eng, unreadable) == (None, {"tier": "x"})
    assert _read(eng, theirs) == (None, {"tier": "y"})
    assert _read(eng, nothing) == (None, None)
    assert _read(eng, nul) == (datetime(2026, 3, 2, tzinfo=utc), {"note": "a\u0000b"})

    _alembic(url, "downgrade", _BEFORE_EVENT_TIME)
    with eng.connect() as c:
        cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'agent_memories'"))}
    assert "event_time" not in cols
    assert _read_meta(eng, only) == {"_event_time": "2026-03-01T00:00:00+00:00"}
    # The same instant, written in UTC: the column kept the instant, not the offset.
    assert _read_meta(eng, mixed) == {"_event_time": "2026-03-01T13:30:00+00:00", "tier": "pro"}
    assert _read_meta(eng, unreadable) == {"tier": "x"}
    assert _read_meta(eng, theirs) == {"tier": "y"}
    assert _read_meta(eng, nothing) is None
    assert _read_meta(eng, nul) == {"_event_time": "2026-03-02T00:00:00+00:00", "note": "a\u0000b"}


def _read_meta(eng, mid):
    import json

    with eng.connect() as c:
        meta = c.execute(text("SELECT metadata::text FROM agent_memories WHERE id = :id"),
                         {"id": mid}).scalar_one()
    return None if meta is None else json.loads(meta)


# ── b64f02d8e9a1: an audit row carries the project it was produced in ──────

_AUDIT_PROJECT = "b64f02d8e9a1"


def test_audit_rows_are_filed_under_the_project_of_their_key(scratch):
    """Existing rows get the project of the key that produced them; a key with
    no project is the account's default project, as `key_project` reads it; a
    row with no key (MCP, before this) stays unfiled rather than guessed."""
    url, eng = scratch
    _alembic(url, "upgrade", _EVENT_TIME)
    tenant, default, beta = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    k_beta, k_none = uuid.uuid4(), uuid.uuid4()
    e_beta, e_none, e_nokey = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with eng.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"), {"id": tenant})
        for pid, name, is_default in ((default, "Default", True), (beta, "Beta", False)):
            c.execute(text("INSERT INTO agent_projects (id, tenant_id, name, slug, is_default)"
                           " VALUES (:id, :t, :n, lower(:n), :d)"),
                      {"id": pid, "t": tenant, "n": name, "d": is_default})
        for kid, pid in ((k_beta, beta), (k_none, None)):
            c.execute(text("INSERT INTO agent_api_keys (id, tenant_id, key_hash, prefix, project_id)"
                           " VALUES (:id, :t, :h, 'kor_self_', :p)"),
                      {"id": kid, "t": tenant, "h": uuid.uuid4().hex, "p": pid})
        for eid, kid in ((e_beta, k_beta), (e_none, k_none), (e_nokey, None)):
            c.execute(text("INSERT INTO audit_events (id, tenant_id, api_key_id, actor, action)"
                           " VALUES (:id, :t, :k, 'rest', 'read')"),
                      {"id": eid, "t": tenant, "k": kid})

    _alembic(url, "upgrade", _AUDIT_PROJECT)
    with eng.connect() as c:
        got = dict(c.execute(text("SELECT id, project_id FROM audit_events")).all())
    assert got == {e_beta: beta, e_none: default, e_nokey: None}

    _alembic(url, "downgrade", _EVENT_TIME)
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM audit_events")).scalar_one() == 3


# ── 2f6c8e1a9d47: the facts drain stamps its own claim ───────────────────

_BEFORE_CLAIM = "7d4e2b9a1c63"
_CLAIM = "2f6c8e1a9d47"


def test_a_claim_in_flight_keeps_its_clock(scratch):
    """The reaper read the claim's age off `updated_at`, which the claim moved
    through `onupdate`. The claim now has a column of its own, and a row the
    old code left 'processing' gets its claim time from where the old code
    wrote it, so the upgrade neither steals live work nor strands a row. Only
    those rows: a memory that is not claimed has no claim time."""
    from datetime import datetime, timezone

    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE_CLAIM)
    tenant, claimed, waiting = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    when = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
    with eng.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"), {"id": tenant})
        for mid, status, updated in ((claimed, "processing", when), (waiting, "pending", None)):
            c.execute(text("INSERT INTO agent_memories"
                           " (id, tenant_id, content, graph_sync_status, updated_at)"
                           " VALUES (:id, :t, 'x', :s, :u)"),
                      {"id": mid, "t": tenant, "s": status, "u": updated})

    _alembic(url, "upgrade", _CLAIM)
    with eng.connect() as c:
        got = dict(c.execute(text(
            "SELECT id, graph_sync_claimed_at FROM agent_memories")).all())
    assert got == {claimed: when, waiting: None}

    _alembic(url, "downgrade", _BEFORE_CLAIM)
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM agent_memories")).scalar_one() == 2


# ── e3a9c5d1f7b2: when the store learned a fact had ended ───────────────────

_BEFORE_CLOSED_KNOWN = "c2e8a4f6d913"
_CLOSED_KNOWN = "e3a9c5d1f7b2"


def test_the_end_of_a_closed_fact_is_dated_once_and_then_by_the_database(scratch):
    """`known_as_of` (11 Oct 2026). The facts closed before the column get it
    once, from what is stored: closed by another fact, when that one was
    learned (never before the closed one); forgotten by itself, its first
    forget; otherwise the earlier of its end and its last change, never
    before it was learned. An open fact gets none. From then on the database
    fills it, and the way back drops it."""
    from datetime import datetime, timezone

    utc = timezone.utc
    d = lambda month, day: datetime(2026, month, day, 9, 0, tzinfo=utc)  # noqa: E731
    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE_CLOSED_KNOWN)
    tenant = uuid.uuid4()
    ids = {name: uuid.uuid4() for name in (
        "closer", "closed", "early_closed", "late_closer", "forgotten", "past", "now_closed", "open")}

    def fact(c, name, *, created, updated=None, invalid=None, by=None):
        c.execute(text(
            "INSERT INTO agent_facts (id, tenant_id, subject_label, subject_canonical,"
            " predicate_raw, predicate_canonical, object_label, object_canonical, confidence,"
            " valid_from, created_at, updated_at, invalid_at, invalidated_by_id)"
            " VALUES (:id, :t, 's', 's', 'p', 'p', :o, :o, 0.9, :vf, :c, :u, :i, :by)"),
            {"id": ids[name], "t": tenant, "o": name, "vf": d(1, 1), "c": created,
             "u": updated or created, "i": invalid, "by": ids[by] if by else None})

    with eng.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"), {"id": tenant})
        fact(c, "closer", created=d(10, 5))
        # Closed by a fact learned on 5 October.
        fact(c, "closed", created=d(8, 1), invalid=d(9, 1), by="closer")
        # Learned after the newer fact that closes it: its end with its start.
        fact(c, "late_closer", created=d(6, 1))
        fact(c, "early_closed", created=d(7, 1), invalid=d(3, 1), by="late_closer")
        # Forgotten by itself on 3 October, with an end date in December.
        fact(c, "forgotten", created=d(8, 1), updated=d(10, 9), invalid=d(12, 31))
        c.execute(text(
            "INSERT INTO agent_forget_audit (id, tenant_id, scope, target_id, facts_invalidated,"
            " created_at) VALUES (:id, :t, 'fact', :target, 1, :at)"),
            {"id": uuid.uuid4(), "t": tenant, "at": d(10, 3),
             "target": "fct_" + ids["forgotten"].hex})
        # Told in the past tense: ended before it was learned.
        fact(c, "past", created=d(9, 15), updated=d(9, 16), invalid=d(5, 1))
        # Closed "now" by a forget of the whole end user on 7 October.
        fact(c, "now_closed", created=d(8, 1), updated=d(10, 8), invalid=d(10, 7))
        fact(c, "open", created=d(8, 1))

    _alembic(url, "upgrade", _CLOSED_KNOWN)
    with eng.connect() as c:
        got = {name: c.execute(text("SELECT closed_known_at FROM agent_facts WHERE id = :id"),
                               {"id": fid}).scalar_one() for name, fid in ids.items()}
    assert got["closed"] == d(10, 5)
    assert got["early_closed"] == d(7, 1)
    assert got["forgotten"] == d(10, 3)
    assert got["past"] == d(9, 15)
    assert got["now_closed"] == d(10, 7)
    assert got["open"] is None and got["closer"] is None

    # From now on the database fills it.
    with eng.begin() as c:
        c.execute(text("UPDATE agent_facts SET invalid_at = :i WHERE id = :id"),
                  {"i": d(11, 1), "id": ids["open"]})
        filled = c.execute(text("SELECT closed_known_at FROM agent_facts WHERE id = :id"),
                           {"id": ids["open"]}).scalar_one()
    assert filled is not None and filled > d(10, 9)

    _alembic(url, "downgrade", _BEFORE_CLOSED_KNOWN)
    with eng.connect() as c:
        cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'agent_facts'"))}
        triggers = c.execute(text(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_agent_facts_closed_known_at'"
        )).scalar_one()
    assert "closed_known_at" not in cols and triggers == 0


# ── a5d81f3c7e64: a pseudonym in what the past erasures left ─────────────────

_PSEUDONYMS = "a5d81f3c7e64"


def test_the_past_erasures_leave_a_pseudonym(scratch, monkeypatch):
    """0.1.27 (services/pseudonyms.py): an erasure made before it left the
    end user's id in clear. Once: up to the erasure (and its own trail row,
    written just after), the records and the trail take the pseudonym the
    code computes, the cost rows lose the end user, a delivered webhook takes
    the pseudonym; what came later (an end user of the same id since) and a
    delivery still to make stay as they are."""
    from datetime import datetime, timedelta, timezone

    from korely_agent.config import get_settings
    from korely_agent.services.pseudonyms import pseudonym

    monkeypatch.setenv("SECRET_KEY", "a-long-random-string-for-the-tests")
    get_settings.cache_clear()
    url, eng = scratch
    _alembic(url, "upgrade", _CLOSED_KNOWN)
    t, ep = uuid.uuid4(), uuid.uuid4()
    at = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    day, later = timedelta(days=1), timedelta(days=7)
    ids = {}

    def trail(c, name, user, ts, action="read"):
        ids[name] = uuid.uuid4()
        c.execute(text("INSERT INTO audit_events (id, ts, tenant_id, actor, action, result,"
                       " end_user_id) VALUES (:id, :ts, :t, 'rest', :a, 'ok', :u)"),
                  {"id": ids[name], "ts": ts, "t": t, "a": action, "u": user})

    def usage(c, name, ts):
        ids[name] = uuid.uuid4()
        c.execute(text("INSERT INTO agent_llm_usage (id, ts, tenant_id, stage, provider, model,"
                       " end_user_id) VALUES (:id, :ts, :t, 'extraction', 'gemini', 'm', 'maria')"),
                  {"id": ids[name], "ts": ts, "t": t})

    def delivery(c, name, status, created):
        ids[name] = uuid.uuid4()
        c.execute(text(
            "INSERT INTO agent_webhook_deliveries (id, tenant_id, endpoint_id, event_type, payload,"
            " status, created_at) VALUES (:id, :t, :ep, 'memory.created', CAST(:p AS json), :s, :c)"),
            {"id": ids[name], "t": t, "ep": ep, "s": status, "c": created,
             "p": '{"event": "memory.created", "data": {"id": "mem_1", "user_id": "maria",'
                  ' "agent_id": null}, "project_id": null}'})

    with eng.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"), {"id": t})
        c.execute(text("INSERT INTO agent_webhook_endpoints (id, tenant_id, url, secret, events)"
                       " VALUES (:id, :t, 'https://hooks.example/x', 'whsec_t',"
                       " CAST('[\"memory.created\"]' AS json))"), {"id": ep, "t": t})
        for name, scope, when in (("forget", "memory", at - day), ("erasure", "user", at)):
            ids[name] = uuid.uuid4()
            c.execute(text("INSERT INTO agent_forget_audit (id, tenant_id, end_user_id, scope,"
                           " created_at) VALUES (:id, :t, 'maria', :s, :c)"),
                      {"id": ids[name], "t": t, "s": scope, "c": when})
        trail(c, "read_before", "maria", at - day)
        trail(c, "erase_request", "maria", at + timedelta(seconds=2), action="erase")
        trail(c, "read_after", "maria", at + later)
        trail(c, "luca", "luca", at - day)
        usage(c, "cost_before", at - day)
        usage(c, "cost_after", at + later)
        delivery(c, "made", "delivered", at - day)
        delivery(c, "waiting", "pending", at - day)

    _alembic(url, "upgrade", _PSEUDONYMS)
    p = pseudonym(t, "maria")
    with eng.connect() as c:
        def one(sql, name):
            return c.execute(text(sql), {"id": ids[name]}).scalar_one()

        def who(name):
            return one("SELECT end_user_id FROM audit_events WHERE id = :id", name)

        assert one("SELECT end_user_id FROM agent_forget_audit WHERE id = :id", "erasure") == p
        assert one("SELECT end_user_id FROM agent_forget_audit WHERE id = :id", "forget") == p
        assert (who("read_before"), who("erase_request")) == (p, p)
        assert who("read_after") == "maria" and who("luca") == "luca"
        assert one("SELECT end_user_id FROM agent_llm_usage WHERE id = :id", "cost_before") is None
        assert one("SELECT end_user_id FROM agent_llm_usage WHERE id = :id", "cost_after") == "maria"
        user_of = "SELECT payload->'data'->>'user_id' FROM agent_webhook_deliveries WHERE id = :id"
        assert one(user_of, "made") == p and one(user_of, "waiting") == "maria"
    _alembic(url, "downgrade", _CLOSED_KNOWN)


# ── b8e4d2f6a9c1: an end never learned before its fact ──────────────────────

_BEFORE_END_AFTER_FACT = "e7c3a9b5d2f4"
_END_AFTER_FACT = "b8e4d2f6a9c1"


def test_an_end_is_never_learned_before_its_fact(scratch):
    """The audit of 17 Oct 2026: the trigger wrote the closing transaction's
    start, which can be before a fact another worker wrote meanwhile. The
    rows so far are put right, the trigger writes the later of the two from
    then on, and the way back puts the old function back."""
    from datetime import datetime, timedelta, timezone

    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE_END_AFTER_FACT)
    tenant, early, final, ahead = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    learned = datetime(2026, 10, 17, 9, 0, 13, tzinfo=timezone.utc)
    future = datetime.now(timezone.utc) + timedelta(days=1)

    def fact(c, fid, created, invalid=None, known=None):
        c.execute(text(
            "INSERT INTO agent_facts (id, tenant_id, subject_label, predicate_raw,"
            " predicate_canonical, object_label, confidence, created_at, invalid_at,"
            " closed_known_at) VALUES (:id, :t, 's', 'p', 'p', 'o', 0.9, :c, :i, :k)"),
            {"id": fid, "t": tenant, "c": created, "i": invalid, "k": known})

    with eng.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"), {"id": tenant})
        fact(c, early, learned, invalid=learned, known=learned - timedelta(seconds=2))
        fact(c, final, learned, invalid=learned, known=learned + timedelta(seconds=5))
        fact(c, ahead, future)

    _alembic(url, "upgrade", _END_AFTER_FACT)
    with eng.begin() as c:
        known = lambda fid: c.execute(  # noqa: E731
            text("SELECT closed_known_at FROM agent_facts WHERE id = :id"), {"id": fid}).scalar_one()
        assert known(early) == learned
        assert known(final) == learned + timedelta(seconds=5)
        c.execute(text("UPDATE agent_facts SET invalid_at = now() WHERE id = :id"), {"id": ahead})
        assert known(ahead) == future, "the trigger wrote a moment before the fact was learned"

    _alembic(url, "downgrade", _BEFORE_END_AFTER_FACT)
    with eng.connect() as c:
        src = c.execute(text("SELECT prosrc FROM pg_proc WHERE proname = 'agent_facts_closed_known_at'")
                        ).scalar_one()
    assert "GREATEST" not in src


# ── c7e2a5f9b3d1: an erasure record keeps the project it was made in ────────

_BEFORE_ERASED_IN = "f2c8a4d6e1b3"
_ERASED_IN = "c7e2a5f9b3d1"


def test_an_erasure_record_keeps_the_project_it_is_filed_under(scratch):
    """Audit of 19 Oct 2026: a project's deletion files its erasure records
    under the default project, and the restore's replay looked there. The rows
    already there take their `project_id`, the account-wide ones none; the way
    back drops the column."""
    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE_ERASED_IN)
    tenant, project = uuid.uuid4(), uuid.uuid4()
    in_project, everywhere = uuid.uuid4(), uuid.uuid4()
    with eng.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"), {"id": tenant})
        c.execute(text("INSERT INTO agent_projects (id, tenant_id, name, slug, is_default)"
                       " VALUES (:id, :t, 'Beta', 'beta', false)"), {"id": project, "t": tenant})
        for rid, pid in ((in_project, project), (everywhere, None)):
            c.execute(text("INSERT INTO agent_forget_audit (id, tenant_id, project_id, scope)"
                           " VALUES (:id, :t, :p, 'user')"), {"id": rid, "t": tenant, "p": pid})

    _alembic(url, "upgrade", _ERASED_IN)
    with eng.connect() as c:
        got = dict(c.execute(text("SELECT id, erased_in_project_id FROM agent_forget_audit")).all())
    assert got == {in_project: project, everywhere: None}

    _alembic(url, "downgrade", _BEFORE_ERASED_IN)
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM agent_forget_audit")).scalar_one() == 2


# ── a9d3f6c2e8b4: the entity aliases of no project go to Default ────────────

_BEFORE_ALIASES_MOVED = "c7e2a5f9b3d1"


def test_an_alias_of_no_project_is_erased_with_its_end_user(scratch, monkeypatch):
    """Audit of 19 Oct 2026: until 0.1.40 POST /admin/aliases without a project
    wrote the alias with none, and an end user's erasure through /v1, behind
    the key's project, never reached it: the names stayed, in clear. After the
    upgrade they are in the Default project, the newer of two that meet there
    stays, and DELETE /v1/users/{id}/memories takes them. An account with no
    Default project keeps its rows as they were."""
    from datetime import datetime, timedelta, timezone

    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from korely_agent.config import get_settings
    from korely_agent.db import get_db
    from korely_agent.main import create_app
    from korely_agent.services import keys as key_service

    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE_ALIASES_MOVED)
    tenant, default, lone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    early = datetime(2026, 10, 1, tzinfo=timezone.utc)
    later = early + timedelta(days=1)
    with eng.begin() as c:
        for t in (tenant, lone):
            c.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"), {"id": t})
        c.execute(text("INSERT INTO agent_projects (id, tenant_id, name, slug, is_default)"
                       " VALUES (:id, :t, 'Default', 'default', true)"), {"id": default, "t": tenant})

        def alias(t, project, user, label, canonical, since):
            c.execute(text(
                "INSERT INTO agent_entity_aliases (id, tenant_id, project_id, end_user_id,"
                " alias_lower, alias_label, canonical_label, since)"
                " VALUES (:id, :t, :p, :u, lower(:a), :a, :c, :s)"),
                {"id": uuid.uuid4(), "t": t, "p": project, "u": user, "a": label,
                 "c": canonical, "s": since})

        alias(tenant, None, "maria", "My Wife", "Maria Rossi", early)
        alias(tenant, None, "bob", "Boss", "Anna Bianchi", later)       # newer than Default's
        alias(tenant, default, "bob", "Boss", "Carla Verdi", early)
        alias(tenant, None, "bob", "The Shop", "Acme Ltd", early)       # older than Default's
        alias(tenant, default, "bob", "The Shop", "Globex", later)
        alias(lone, None, "maria", "My Wife", "Maria Rossi", early)

    _alembic(url, "upgrade", "head")
    with eng.connect() as c:
        rows = set(c.execute(text(
            "SELECT tenant_id, project_id, end_user_id, alias_label, canonical_label"
            " FROM agent_entity_aliases")).all())
    assert rows == {(tenant, default, "maria", "My Wife", "Maria Rossi"),
                    (tenant, default, "bob", "Boss", "Anna Bianchi"),
                    (tenant, default, "bob", "The Shop", "Globex"),
                    (lone, None, "maria", "My Wife", "Maria Rossi")}

    monkeypatch.setenv("SECRET_KEY", "x" * 48)
    monkeypatch.setenv("RUN_WORKER", "false")
    get_settings.cache_clear()
    session = sessionmaker(bind=eng)()
    try:
        _row, full = key_service.create_api_key(session, tenant_id=tenant, name="agent",
                                                project_id=default, expires_in_days=None)
        app = create_app()
        app.dependency_overrides[get_db] = lambda: session
        with TestClient(app) as client:
            r = client.delete("/v1/users/maria/memories",
                              headers={"Authorization": f"Bearer {full}"})
        assert r.status_code == 200, r.text
    finally:
        session.close()
        get_settings.cache_clear()
    with eng.connect() as c:
        left = set(c.execute(text(
            "SELECT tenant_id, end_user_id, alias_label FROM agent_entity_aliases")).all())
    assert left == {(tenant, "bob", "Boss"), (tenant, "bob", "The Shop"),
                    (lone, "maria", "My Wife")}, "the erased end user's names are still there"


# ── d8b4e1a7c3f5: the erasure records keep their key, the September ones their project ─
# Audit of 20 Oct 2026. The end-user erasures made under 0.1.1-0.1.11 wrote
# their record with no project, and a5d81f3c7e64 read that as "rows of no
# project", so the erased person's trail rows, filed under the key's project,
# kept the id in clear. And no record said which pseudonym key it was made
# under, so a restore's replay under another key failed open.

_BEFORE, _THIS = "a9d3f6c2e8b4", "d8b4e1a7c3f5"


@pytest.fixture
def keyed(monkeypatch):
    """SECRET_KEY set for the migration and the test alike."""
    monkeypatch.setenv("SECRET_KEY", "a-long-random-string-for-the-tests")
    from korely_agent.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_a_september_erasure_takes_its_project_and_the_key_check(scratch, keyed):
    from datetime import datetime, timedelta, timezone

    from korely_agent.services.pseudonyms import key_check, pseudonym

    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    t, proj = uuid.uuid4(), uuid.uuid4()
    at = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    p, p_bob = pseudonym(t, "acceptance-sept"), pseudonym(t, "acceptance-bob")
    ids = {k: uuid.uuid4() for k in ("rec", "add", "erase", "later", "bob_rec", "other")}

    def trail(c, name, user, ts, action, project=proj):
        c.execute(text("INSERT INTO audit_events (id, ts, tenant_id, project_id, actor, action,"
                       " result, end_user_id) VALUES (:id, :ts, :t, :pr, 'rest', :a, 'ok', :u)"),
                  {"id": ids[name], "ts": ts, "t": t, "pr": project, "a": action, "u": user})

    with eng.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"), {"id": t})
        c.execute(text("INSERT INTO agent_projects (id, tenant_id, name, slug, is_default)"
                       " VALUES (:id, :t, 'Default', 'default', true)"), {"id": proj, "t": t})
        # As 0.1.11 wrote it and a5d81f3c7e64 left it: the pseudonym, no project.
        for name, who, when in (("rec", p, at), ("bob_rec", p_bob, at + timedelta(days=3))):
            c.execute(text("INSERT INTO agent_forget_audit (id, tenant_id, end_user_id, scope,"
                           " created_at) VALUES (:id, :t, :u, 'user', :c)"),
                      {"id": ids[name], "t": t, "u": who, "c": when})
        trail(c, "add", "acceptance-sept", at - timedelta(days=1), "write")
        trail(c, "erase", "acceptance-sept", at + timedelta(seconds=1), "erase")
        trail(c, "later", "acceptance-sept", at + timedelta(days=30), "read")
        # Bob's record is account-wide: its own row names the pseudonym.
        trail(c, "other", p_bob, at + timedelta(days=3, seconds=1), "erase", project=None)

    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        def one(sql, name):
            return c.execute(text(sql), {"id": ids[name]}).scalar_one()

        who = "SELECT end_user_id FROM audit_events WHERE id = :id"
        assert (one(who, "add"), one(who, "erase")) == (p, p), "the September trail kept the id"
        assert one(who, "later") == "acceptance-sept", "a row after the erasure is not this one's"
        rec = "SELECT erased_in_project_id FROM agent_forget_audit WHERE id = :id"
        assert one(rec, "rec") == proj and one(rec, "bob_rec") is None
        check = "SELECT key_check FROM agent_forget_audit WHERE id = :id"
        assert one(check, "rec") == key_check() and one(check, "bob_rec") == key_check()
    _alembic(url, "downgrade", _BEFORE)


# ── c3f7a9e5b1d8: the memory that wrote a fact first ─────────────────────────

_BEFORE_FIRST_WRITER, _FIRST_WRITER = "e7c4b2d9a6f1", "c3f7a9e5b1d8"


def test_the_first_writer_is_kept_from_the_insert_on(scratch):
    """Audit before 1.0.0: a stored fact takes its `source_memory_id` as its
    first writer; from then on the database sets it at the insert, and a forget
    or an edit that re-points the source leaves it; the way back drops it."""
    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE_FIRST_WRITER)
    tenant, m1, m2, old, new = (uuid.uuid4() for _ in range(5))

    def fact(c, fid, source):
        c.execute(text(
            "INSERT INTO agent_facts (id, tenant_id, subject_label, subject_canonical,"
            " predicate_raw, predicate_canonical, object_label, object_canonical, confidence,"
            " source_memory_id) VALUES (:id, :t, 's', 's', 'p', 'p', :o, :o, 0.9, :m)"),
            {"id": fid, "t": tenant, "m": source, "o": str(fid)})

    with eng.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"), {"id": tenant})
        for m in (m1, m2):
            c.execute(text("INSERT INTO agent_memories (id, tenant_id, content)"
                           " VALUES (:id, :t, 'dummy')"), {"id": m, "t": tenant})
        fact(c, old, m1)
    _alembic(url, "upgrade", _FIRST_WRITER)

    def first(fid):
        with eng.connect() as c:
            return c.execute(text("SELECT first_memory_id FROM agent_facts WHERE id = :id"),
                             {"id": fid}).scalar_one()

    assert first(old) == m1
    with eng.begin() as c:
        fact(c, new, m1)
        c.execute(text("UPDATE agent_facts SET source_memory_id = :m"), {"m": m2})
    assert first(new) == m1 and first(old) == m1
    with eng.begin() as c:
        c.execute(text("UPDATE agent_facts SET first_memory_id = NULL WHERE id = :id"),
                  {"id": new})
        c.execute(text("DELETE FROM agent_memories WHERE id = :id"), {"id": m1})
    assert first(old) is None, "the deleted memory's id stays nowhere"

    _alembic(url, "downgrade", _BEFORE_FIRST_WRITER)
    with eng.connect() as c:
        cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'agent_facts'"))}
        triggers = c.execute(text(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_agent_facts_first_memory_id'"
        )).scalar_one()
    assert "first_memory_id" not in cols and triggers == 0


def test_two_processes_upgrade_one_new_database(scratch):
    """Two APIs starting on one new database run the entrypoint's upgrade at
    the same moment. Before alembic/env.py's lock the second died on the
    first one's `alembic_version` (30 Oct 2026); now it waits, finds head
    and has nothing to do."""
    url, eng = scratch
    # A second try starts from a new database again: the one that did not
    # lose its working directory went to head alone.
    runs = _alembic_runs(url, ("upgrade", "head"), ("upgrade", "head"),
                         start_again=lambda: _new_database(url, eng))
    for r in runs:
        assert r.returncode == 0, f"one upgrade failed:\n{r.stderr[-3000:]}"
    with eng.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [_chain()[-1]]
