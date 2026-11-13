"""The suite in several processes (`make test-all WORKERS=N`, pytest-xdist).

Every test of the suite drops, creates and empties the tables of the database
it is given, so the workers of one run cannot share one: each has its own,
named after the run's and the worker's id, made when missing and dropped at
the end (tests/conftest.py, `own_database`). The application reads
DATABASE_URL, which moves with it. The tests that need the machine to
themselves are marked `serial` and run after, in one process; in a worker they
are an error, never a pass on luck and never a skip.
"""
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import psycopg2.extensions
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.conftest import _the_workers, server_url, workers_urls

ROOT = Path(__file__).resolve().parent.parent
RUN = "postgresql://korely:test@localhost:55433/korely_test_r108"


def _name(url):
    return make_url(url).database


def test_a_workers_database_is_the_runs_name_and_its_id():
    test_db, app = workers_urls("gw3", RUN, RUN)
    assert _name(test_db) == _name(app) == "korely_test_r108_gw3"
    u, run = make_url(test_db), make_url(RUN)
    assert (u.username, u.password, u.host, u.port) == (run.username, run.password, run.host, run.port)


@pytest.mark.parametrize("app", [
    "postgresql://nobody@127.0.0.1:1/nothing",            # the placeholder of a run with no database
    "postgresql://korely:test@localhost:55433/korely_other",
    "postgresql://korely:test@localhost:5432/korely_test_r108",
])
def test_the_application_moves_only_from_the_runs_own_database(app):
    test_db, moved = workers_urls("gw0", RUN, app)
    assert _name(test_db) == "korely_test_r108_gw0"
    assert moved == app


def test_the_same_database_under_another_driver_name_moves_too():
    _test_db, moved = workers_urls("gw1", RUN, RUN.replace("postgresql://", "postgresql+psycopg2://"))
    assert _name(moved) == "korely_test_r108_gw1"
    assert make_url(moved).drivername == "postgresql+psycopg2"


def test_a_run_name_too_long_for_its_workers_is_refused():
    """Postgres cuts a name at 63 bytes without a word: two workers would
    share the cut name."""
    long = "korely_test_" + "x" * 48           # 60 bytes: _gw0 makes 64
    with pytest.raises(pytest.UsageError, match="too long"):
        _the_workers(f"postgresql://korely:test@localhost:55433/{long}", "gw0")
    assert _name(_the_workers(f"postgresql://k@h/{long[:59]}", "gw0")).endswith("_gw0")


_INNER = '''
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def _seen(own_database, test):
    name = make_url(own_database).database
    eng = create_engine(own_database)
    with eng.connect() as c:
        here = c.execute(text("SELECT current_database()")).scalar()
    eng.dispose()
    app = make_url(os.environ["DATABASE_URL"]).database
    worker = os.environ["PYTEST_XDIST_WORKER"]
    Path(os.environ["SEEN"], f"{test}-{worker}-{name}").write_text(f"{here} {app}")


def test_one(own_database):
    _seen(own_database, "one")


def test_two(own_database):
    _seen(own_database, "two")


@pytest.mark.serial
def test_alone():
    pass
'''


def test_each_worker_has_a_database_of_its_own_and_leaves_none(own_database, tmp_path):
    """A real run in two workers, on a run name no other run has. The
    environment says it is worker gw9 of some other run, as a process a
    worker starts would: the run itself is not a worker, and its workers are
    the ones xdist names."""
    run = f"korely_test_xd_{uuid.uuid4().hex[:8]}"
    url = make_url(own_database).set(database=run).render_as_string(hide_password=False)
    (tmp_path / "test_inner.py").write_text(_INNER)
    seen = tmp_path / "seen"
    seen.mkdir()
    report = tmp_path / "report.xml"
    env = {**os.environ, "TEST_DATABASE_URL": url, "DATABASE_URL": url, "SEEN": str(seen),
           "PYTEST_XDIST_WORKER": "gw9", "PYTHONPATH": str(ROOT)}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(ROOT / "pyproject.toml"),
         "-p", "tests.conftest", "-n", "2", f"--junitxml={report}", str(tmp_path / "test_inner.py")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    out = r.stdout[-3000:] + r.stderr[-3000:]

    cases = {c.get("name"): c for c in ET.parse(report).getroot().iter("testcase")}
    assert set(cases) == {"test_one", "test_two", "test_alone"}, out
    for name in ("test_one", "test_two"):
        assert not list(cases[name]), f"{name}: {out}"
    alone = cases["test_alone"]
    assert [e.tag for e in alone] == ["error"], out
    assert "marked serial" in alone[0].get("message", "") + (alone[0].text or ""), out

    found = sorted(p.name for p in seen.iterdir())
    assert len(found) == 2, found
    for f in seen.iterdir():
        _test, worker, name = f.name.split("-", 2)
        assert worker in ("gw0", "gw1") and name == f"{run}_{worker}", f.name
        assert f.read_text() == f"{name} {name}", "the test and the application met in its database"

    admin = create_engine(server_url(own_database), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            left = c.execute(text("SELECT datname FROM pg_database WHERE datname LIKE :p"),
                             {"p": f"{run}%"}).scalars().all()
    finally:
        admin.dispose()
    assert left == [], f"left behind: {left}"
    assert r.returncode == 1, out      # the serial test's error, and only that


# `make test-all` runs scripts/test_all.sh in the test image, WORKERS from the
# environment. Here a `python` of our own answers for pytest: it writes down
# its arguments and exits with the next status it is given.
_FAKE_PYTHON = '''#!/bin/sh
n=$(wc -l < "$CALLS")
echo "$*" >> "$CALLS"
[ "$n" = "${SIGNAL_ON_CALL:-none}" ] && kill -"$SIGNAL" "$PPID"
i=0
for status in $STATUSES; do
    [ "$i" -eq "$n" ] && exit "$status"
    i=$((i + 1))
done
exit 0
'''


def _test_all(tmp_path, workers, statuses="", **signal):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "python").write_text(_FAKE_PYTHON)
    (bin_ / "python").chmod(0o755)
    calls = tmp_path / "calls"
    calls.write_text("")
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "CALLS": str(calls), "STATUSES": statuses,
           **{k.upper(): str(v) for k, v in signal.items()}}
    if workers is not None:
        env["WORKERS"] = workers
    r = subprocess.run(["sh", str(ROOT / "scripts" / "test_all.sh")], env=env,
                       capture_output=True, text=True, timeout=60)
    return r.returncode, calls.read_text().splitlines(), r.stdout + r.stderr


@pytest.mark.parametrize("workers", [None, ""])
def test_with_no_workers_the_suite_runs_in_one_process_as_it_did(tmp_path, workers):
    """The release runs `make test-all` with no WORKERS (scripts/release.sh)."""
    assert _test_all(tmp_path, workers, "3")[:2] == (3, ["-m pytest -q"])


def test_with_workers_the_serial_tests_run_after_in_one_process(tmp_path):
    code, calls, _ = _test_all(tmp_path, "4")
    assert (code, calls) == (0, ["-m pytest -q -n 4 --maxschedchunk 1 -m not serial",
                                 "-m pytest -q -m serial"])


@pytest.mark.parametrize("statuses, code", [
    ("1 0", 1), ("0 1", 1), ("2 1", 2),
    ("0 5", 0),      # no test marked serial: pytest's "no tests ran" is not a failure
    ("5 0", 5),      # a parallel pass with nothing to run is
])
def test_both_passes_run_and_both_must_pass(tmp_path, statuses, code):
    got, calls, out = _test_all(tmp_path, "6", statuses)
    assert got == code, out
    assert len(calls) == 2, "the serial pass runs whatever the parallel one gave"


@pytest.mark.parametrize("workers", ["abc", "0", "04", "-1", "4; echo pwned", "4 -p x", "auto"])
def test_anything_but_a_number_of_processes_is_refused(tmp_path, workers):
    code, calls, out = _test_all(tmp_path, workers)
    assert (code, calls) == (2, []), out
    assert "refused" in out


@pytest.mark.parametrize("signal, status", [("INT", 130), ("TERM", 143)])
def test_a_stopped_parallel_pass_starts_no_serial_pass(tmp_path, signal, status):
    """A Ctrl-C reaches the script with the parallel pass (the container's
    init hands it to every process of the run): the run stops there, with
    the signal's status."""
    code, calls, out = _test_all(tmp_path, "4", "2", signal_on_call=0, signal=signal)
    assert (code, len(calls)) == (status, 1), out
    assert "serial pass does not run" in out


def _test_all_recipe():
    makefile = (ROOT / "Makefile").read_text()
    return makefile, makefile.split("\ntest-all:", 1)[1].split("\n\n", 1)[0]


def test_the_makefile_takes_workers_from_its_command_line_only():
    """Never pasted into the command line: the value reaches the script as a
    variable, and the script refuses what is not a number. And never from the
    environment (`:=`): a WORKERS left in a shell would turn the release's
    `make test-all` (scripts/release.sh) and the CI's into a parallel run."""
    makefile, recipe = _test_all_recipe()
    assert "\nWORKERS :=\nexport WORKERS\n" in makefile and "WORKERS ?=" not in makefile
    assert "-e WORKERS \\" in recipe and recipe.rstrip().endswith("sh scripts/test_all.sh")
    assert "$(WORKERS)" not in makefile


def test_the_run_is_under_an_init_that_hands_a_ctrl_c_to_all_of_it():
    """A shell as PID 1 ignores a Ctrl-C; Docker's init hands it to every
    process of the run, and `docker stop` sends the same SIGINT, which ends
    pytest and its workers as at a terminal (a TERM ends a Python process on
    the spot, its database left behind)."""
    _makefile, recipe = _test_all_recipe()
    for option in ("--init", "--stop-signal SIGINT", "-e TINI_KILL_PROCESS_GROUP=1"):
        assert option in recipe, option


def test_the_run_writes_no_bytecode():
    """Every run compiles what it imports, in its copy of the checkout
    (tests/test_the_suite_runs_from_a_copy.py), as the runs measured on
    6-7 Nov 2026 did. Put there on 7 Nov for a process that lost its working
    directory while bytecode was written into the mounted checkout; the cause
    was Docker Desktop's file sharing while containers start or stop
    (scripts/from_a_copy.sh), which the copy removes."""
    _makefile, recipe = _test_all_recipe()
    assert "-e PYTHONDONTWRITEBYTECODE=1" in recipe


def _refused_by_make() -> re.Pattern:
    """The second rule of the Makefile's test-db: a name it refuses."""
    recipe = (ROOT / "Makefile").read_text().split("\ntest-db:", 1)[1].split("\n\n", 1)[0]
    m = re.search(r"! printf '%s\\n' \"\$\$TEST_DB\" \| grep -Eq '([^']+)\$\$'", recipe)
    assert m, "test-db refuses a worker's name"
    return re.compile(m.group(1) + "$")


@pytest.mark.parametrize("name, refused", [
    ("korely_test_x_gw0", True), ("korely_test_x_gw12", True),
    ("korely_test_x", False), ("korely_test_gwx", False), ("korely_test_x_gw", False),
])
def test_a_run_named_like_a_worker_is_refused(name, refused):
    """A run named korely_test_x_gw0 would be the database of worker gw0 of a
    parallel run named korely_test_x, which drops it at its end (seen in
    review, 7 Nov 2026). make refuses it, and so does the suite run by hand."""
    from tests.conftest import _A_WORKERS_DATABASE

    assert bool(_refused_by_make().search(name)) is refused
    assert bool(_A_WORKERS_DATABASE.fullmatch(name)) is refused


def test_the_suite_by_hand_refuses_a_run_named_like_a_worker():
    url = "postgresql://korely:test@localhost:55433/korely_test_x_gw3"
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--collect-only",
                        "tests/test_one_number_per_limit.py"], cwd=ROOT, capture_output=True, text=True,
                       timeout=300, env={**os.environ, "TEST_DATABASE_URL": url, "DATABASE_URL": url})
    assert r.returncode == 4, r.stdout[-2000:] + r.stderr[-2000:]
    assert "ends the way a worker's database" in r.stdout + r.stderr


def _databases(admin, prefix):
    with admin.connect() as c:
        return set(c.execute(text("SELECT datname FROM pg_database WHERE starts_with(datname, :p)"),
                             {"p": prefix}).scalars())


def test_the_controller_drops_what_its_workers_left_and_nothing_else(own_database, tmp_path):
    """A worker stopped halfway leaves its database (a Ctrl-C after a minute
    left one of two, 7 Nov 2026); the controller, which ends after its
    workers, drops every <run>_gw<n>, and only those."""
    run = f"korely_test_xc_{uuid.uuid4().hex[:8]}"
    left, kept = f"{run}_gw7", {f"{run}_kept", f"{run}2_gw0", f"{run}_gwx"}
    admin = create_engine(server_url(own_database), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            for name in (left, *kept):
                c.execute(text(f'CREATE DATABASE "{name}"'))
        (tmp_path / "test_nothing.py").write_text("def test_nothing():\n    pass\n")
        url = make_url(own_database).set(database=run).render_as_string(hide_password=False)
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(ROOT / "pyproject.toml"),
             "-p", "tests.conftest", "-n", "1", str(tmp_path / "test_nothing.py")],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
            env={**os.environ, "TEST_DATABASE_URL": url, "DATABASE_URL": url, "PYTHONPATH": str(ROOT)})
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        assert _databases(admin, run) == kept
    finally:
        with admin.connect() as c:
            for name in (left, *kept):
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


class _ClosesLate(psycopg2.extensions.connection):
    """A connection whose close reaches the server `LATE` seconds after the
    pool has closed it: the backend of a closed session still in the
    database when the drop looks, which the CI's runner met by itself on
    every run from 10 Nov 2026, here every time."""
    LATE = 0.5

    def close(self):
        threading.Timer(self.LATE, psycopg2.extensions.connection.close, args=(self,)).start()


@pytest.mark.parametrize("closes_late", [False, True], ids=["closed_at_once", "closed_late"])
def test_a_workers_drop_ends_none_of_the_applications_connections(own_database, monkeypatch,
                                                                  closes_late):
    """The application's engines keep their connections in their pools for
    the life of the process: the drop closes them first, and WITH (FORCE)
    ends only what some test left open, which `drop_a_runs_database` counts.
    Without the closing the first count is not 0. A closed connection whose
    backend has not left the server yet is not one left open (`closed_late`):
    counted right after the close, the CI's runner saw 2 for 1."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    from korely_agent.config import get_settings
    from korely_agent.db import base

    from tests.conftest import drop_a_runs_database

    name = f"korely_test_dr_{uuid.uuid4().hex[:8]}"
    admin = create_engine(server_url(own_database), isolation_level="AUTOCOMMIT")
    url = make_url(own_database).set(database=name).render_as_string(hide_password=False)

    def closing_late(dialect, conn_rec, cargs, cparams):
        if cparams.get("dbname") == name:
            cparams["connection_factory"] = _ClosesLate

    if closes_late:
        event.listen(Engine, "do_connect", closing_late)
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        for keep_one_open in (False, True):
            if keep_one_open:
                with admin.connect() as c:
                    c.execute(text(f'CREATE DATABASE "{name}"'))
            monkeypatch.setenv("DATABASE_URL", url)
            get_settings.cache_clear()
            for cached in (base.get_engine, base.get_own_engine, base.get_sessionmaker,
                           base.get_own_sessionmaker):
                cached.cache_clear()
            with base.get_engine().connect() as c:
                c.execute(text("SELECT 1"))
            with base.get_own_engine().connect() as c:
                c.execute(text("SELECT 1"))
            left_open = create_engine(url).connect() if keep_one_open else None
            assert len(_sessions(admin, name)) >= 2 + keep_one_open
            assert drop_a_runs_database(admin, name) == (1 if keep_one_open else 0)
            assert _databases(admin, name) == set()
            if left_open is not None:
                left_open.invalidate()
    finally:
        if closes_late:
            event.remove(Engine, "do_connect", closing_late)
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_a_drop_given_up_on_for_another_reason_is_not_forced(own_database):
    """Only object_in_use, a session that stayed, leads to WITH (FORCE): a
    drop without FORCE cancelled while it waits for a session left open
    (query_canceled) is raised, and the database is still there."""
    from sqlalchemy.exc import OperationalError

    from tests.conftest import drop_a_runs_database

    name = f"korely_test_dc_{uuid.uuid4().hex[:8]}"
    admin = create_engine(server_url(own_database), isolation_level="AUTOCOMMIT")
    url = make_url(own_database).set(database=name).render_as_string(hide_password=False)

    def cancel_the_wait():
        """The drop of `name` without FORCE, once it waits: cancelled."""
        until = time.monotonic() + 5
        with admin.connect() as c:
            while time.monotonic() < until:
                pid = c.execute(text(
                    "SELECT pid FROM pg_stat_activity WHERE state = 'active' AND "
                    "starts_with(query, 'DROP DATABASE') AND position(:n in query) > 0 AND "
                    "position('FORCE' in query) = 0"), {"n": name}).scalar()
                if pid is not None:
                    c.execute(text("SELECT pg_cancel_backend(:p)"), {"p": pid})
                    return
                time.sleep(0.01)

    left_open = None
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        left_open = create_engine(url).connect()
        canceller = threading.Thread(target=cancel_the_wait)
        canceller.start()
        with pytest.raises(OperationalError) as e:
            drop_a_runs_database(admin, name)
        canceller.join()
        assert e.value.orig.pgcode == "57014"      # query_canceled, not object_in_use
        assert _databases(admin, name) == {name}
    finally:
        if left_open is not None:
            left_open.invalidate()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_the_drop_counts_the_sessions_not_their_workers(own_database):
    """What stays in a database after the drop's wait is counted by its
    clients' sessions: a session left open in the middle of a parallel
    query is one, though its worker is in the database too (as an
    autovacuum worker can be, 3 runs of 10 on two processors)."""
    from sqlalchemy.exc import DBAPIError

    from tests.conftest import drop_a_runs_database

    name = f"korely_test_dp_{uuid.uuid4().hex[:8]}"
    admin = create_engine(server_url(own_database), isolation_level="AUTOCOMMIT")
    url = make_url(own_database).set(database=name).render_as_string(hide_password=False)
    left_open = None

    def a_long_parallel_query():
        try:
            left_open.execute(text("SET debug_parallel_query = on"))
            left_open.execute(text("SELECT pg_sleep(60)"))
        except DBAPIError:
            pass                                   # ended by the drop's FORCE

    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        left_open = create_engine(url, isolation_level="AUTOCOMMIT").connect()
        query = threading.Thread(target=a_long_parallel_query)
        query.start()
        until = time.monotonic() + 30
        while "parallel worker" not in _backend_types(admin, name):
            assert time.monotonic() < until, _backend_types(admin, name)
            time.sleep(0.05)
        assert drop_a_runs_database(admin, name) == 1
        query.join(30)
        assert _databases(admin, name) == set()
    finally:
        if left_open is not None:
            left_open.invalidate()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _backend_types(admin, name):
    with admin.connect() as c:
        return c.execute(text("SELECT backend_type FROM pg_stat_activity WHERE datname = :n"),
                         {"n": name}).scalars().all()


def _sessions(admin, name):
    with admin.connect() as c:
        return c.execute(text("SELECT pid FROM pg_stat_activity WHERE datname = :n"),
                         {"n": name}).scalars().all()


def test_a_test_that_points_the_app_elsewhere_leaves_the_next_test_its_database(own_database):
    """tests/test_blind_install.py builds the app with DATABASE_URL on a
    server that is not there; the test with the database after it, in the
    same process, was refused (7 of tests/test_recompute_vectors.py, 54 in a
    run in four processes) until the engines on another database are
    dropped between tests (tests/conftest.py). The pair, in a process of
    its own, on a database of its own."""
    name = f"korely_test_sb_{uuid.uuid4().hex[:8]}"
    admin = create_engine(server_url(own_database), isolation_level="AUTOCOMMIT")
    url = make_url(own_database).set(database=name).render_as_string(hide_password=False)
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "tests/test_blind_install.py::TestAFailingExportIsVisible::"
             "test_an_install_that_exports_nothing_reports_nothing",
             "tests/test_recompute_vectors.py::TestADryRun::test_counts_and_writes_nothing"],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
            env={**os.environ, "TEST_DATABASE_URL": url, "DATABASE_URL": url})
        assert r.returncode == 0, r.stdout[-3000:]
        assert "2 passed" in r.stdout, r.stdout[-3000:]
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_every_test_starts_with_the_records_of_a_new_process(request):
    """The records /health reports and the drain decides on, which
    tests/conftest.py puts back before each test: every one of them left
    failing, then as a new process has them. And the fixture that does it
    runs for every test."""
    from datetime import datetime, timezone

    from korely_agent import worker
    from korely_agent.core import llm, local_embedder
    from korely_agent.services import bookkeeping
    from korely_agent.services.facts import drain

    from tests.conftest import as_a_new_process

    assert "_every_test_starts_with_the_records_of_a_new_process" in request.fixturenames
    now = datetime.now(timezone.utc)
    llm._last_call.update(ok=False, at=1.0, error="APIError: 402", status=402)
    llm._last_embedding.update(ok=False, at=1.0, error="APIError: 500")
    local_embedder._last.update(ok=False, at=1.0, error="OSError")
    worker._job_state["webhooks"] = {"ok": False, "at": now, "detail": "OSError"}
    worker._export_state.update(ok=False, at=now, detail="OSError")
    drain._pauses["global"] = {"until": 9e18, "step": 3, "since": 1.0, "status": 402}
    for ledger in (bookkeeping.COSTS, bookkeeping.AUDIT, bookkeeping.GIVEN_BACK):
        ledger.lost(1, OSError())
    assert llm.provider_state()["state"] == "failing" and drain.writers_refused()

    as_a_new_process()
    assert llm.provider_state()["state"] == "unknown"
    assert llm.embedding_state()["state"] == "unknown"
    assert local_embedder.state()["state"] == "unknown"
    assert {j["state"] for j in worker.jobs_state().values()} == {"unknown"}
    assert worker.export_state()["state"] == "unknown"
    assert drain.writers_refused() == {}
    assert {r["state"] for r in bookkeeping.state().values()} == {"unknown"}
