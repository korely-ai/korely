"""Test fixtures.

One real Postgres, one schema created from the models, one transaction per test
rolled back at the end. No SQLite: this service ships on Postgres with pgvector
and a vector column has no SQLite equivalent, so testing elsewhere would mean
testing something else.
"""
import json
import os
import re
import socket
import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import Pool

# Hermetic tests (2026-09-28). The development image installs `google-genai`
# with the rest of the product's requirements, so a test that sets a key and
# leaves one model call real reaches Google with it: money on every run, and a
# result that depends on somebody else's uptime. Two walls. The provider keys
# are emptied here, before anything imports the settings, so a key in the
# environment or in a `.env` next to the code (the repository is mounted as the
# working directory, and the settings read `.env` from there; an environment
# variable wins over it) cannot reach a test: every path takes its "no
# provider" branch unless the test sets a key itself. And the network is closed
# below to anything but this machine, for whatever does not ask the settings.
# OPENROUTER_API_KEY since 1.0.36: the decisions model of `manage
# review-subjects` (core/llm.DecisionsProvider). tests/test_the_suite_is_
# hermetic.py fails on a setting named *_API_KEY left out of this list.
PROVIDER_KEYS = ("GOOGLE_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY")
for _provider_key in PROVIDER_KEYS:
    os.environ[_provider_key] = ""
# The suite's embedder is Gemini with no key, so no vectors unless a test
# fakes them: the default since 0.1.19 is the local model, which a test run has
# no copy of, and the image built with it says local in its environment. The
# tests of the local embedder and of the default set it themselves.
os.environ["EMBEDDING_PROVIDER"] = "gemini"
# `make test` runs with no database and no DATABASE_URL, and the settings
# cannot be read without one: every unit test that built the app or read a
# setting was red there, 21 failures and 37 errors (audit of 17 Oct 2026).
# So a run without one gets an address where nothing answers: reading the
# settings works, and a test that does reach a database is refused at once
# and shows itself, instead of passing on whatever the first test cached in
# the process's engine (db/base.py). A run with a database keeps its own.
if not os.environ.get("DATABASE_URL"):
    os.environ["DATABASE_URL"] = "postgresql://nobody@127.0.0.1:1/nothing"

TEST_DB = os.getenv(
    "TEST_DATABASE_URL", "postgresql://korely:test@localhost:55433/korely_test"
)

# The suite in several processes (pytest-xdist: `make test-all WORKERS=N`).
# The suite empties the whole schema of the database it is given, makes its
# tables again and empties them after each test (`engine`, `db` below), so N
# processes on one database empty each other's tables halfway through a test,
# the way two runs on one name did
# (tests/test_the_test_database_is_named_by_the_run.py). So
# each worker has its own: the run's name and the worker's id
# (korely_test_r108_gw0, korely_test_r108_gw1, ...), made when missing and
# dropped when the worker ends (`own_database`). One process: WORKER is None
# and everything is as it was.
WORKER = None
_MAINTENANCE_DATABASE = "postgres"
# xdist names its workers gw0, gw1, ... (its docs, PYTEST_XDIST_WORKER), so a
# worker's database is <run>_gw<n>; no run of its own may be named so (the
# Makefile's test-db refuses it too).
_A_WORKERS_DATABASE = re.compile(r".+_gw[0-9]+")
# The database the application is given for the whole run (DATABASE_URL once
# a worker has moved): `_the_applications_engine_is_the_runs` below.
_RUN_DATABASE_URL = None


def pytest_configure(config):
    """A worker of a parallel run moves to its own database.

    The worker is the one xdist names in `config.workerinput` (its
    "workerid"), set on a worker's config only, before this hook runs. Not
    PYTEST_XDIST_WORKER: a worker's environment carries it into every process
    the worker starts, and a pytest started there (a test of the suite's own
    rules) would take itself for that worker. The application reads
    DATABASE_URL, and `make test-all` points it at the run's database too: when
    it does, it moves with TEST_DB, so a route and the fixture that seeded its
    rows meet in one database; a process a test starts inherits it. Test
    modules read TEST_DB when they are imported, at collection, after this.
    TEST_DATABASE_URL keeps the run's name."""
    global TEST_DB, WORKER, _RUN_DATABASE_URL
    run = make_url(TEST_DB).database or ""
    if _A_WORKERS_DATABASE.fullmatch(run):
        raise pytest.UsageError(
            f"TEST_DB {run!r} ends the way a worker's database of a parallel run is named "
            "(<run>_gw<n>): a parallel run of the shorter name would take it as its "
            "worker's and drop it at the end. Name the run otherwise")
    WORKER = getattr(config, "workerinput", {}).get("workerid")
    if WORKER:
        TEST_DB, os.environ["DATABASE_URL"] = workers_urls(
            WORKER, TEST_DB, os.environ["DATABASE_URL"])
    _RUN_DATABASE_URL = os.environ["DATABASE_URL"]


def _same_database(a, b) -> bool:
    """The same database of the same server (host, port, name), whatever the
    driver or the credentials the two URLs are written with."""
    ua, ub = make_url(a), make_url(b)
    return (ua.host, ua.port, ua.database) == (ub.host, ub.port, ub.database)


def pytest_unconfigure(config):
    """The controller of a parallel run drops the databases its workers left.

    A worker drops its own when it ends (`own_database`), and one that is
    stopped halfway, by a Ctrl-C or a crash, does not: a run stopped after a
    minute in two processes left one of its two (7 Nov 2026). The controller
    ends after its workers, Ctrl-C or not, and drops every <run>_gw<n> still
    there. Only those: names a run of its own may not take (pytest_configure,
    the Makefile's test-db). WITH (FORCE) for a worker that is still going,
    which is this run's own. Nothing to do with no server: a run with no
    database (`make test`) asked it for nothing."""
    if WORKER or getattr(config.option, "dist", "no") == "no":
        return
    from sqlalchemy.exc import OperationalError

    run = make_url(TEST_DB).database or ""
    server = create_engine(server_url(), isolation_level="AUTOCOMMIT",
                           connect_args={"connect_timeout": 5})
    try:
        with server.connect() as c:
            left = c.execute(text("SELECT datname FROM pg_database WHERE starts_with(datname, :p)"),
                             {"p": f"{run}_gw"}).scalars().all()
        for name in left:
            if re.fullmatch(re.escape(run) + r"_gw[0-9]+", name):
                drop_a_runs_database(server, name)
    except OperationalError:
        pass
    finally:
        server.dispose()


def workers_urls(worker: str, test_db: str, database_url: str) -> tuple:
    """(TEST_DB, DATABASE_URL) for `worker`, given the run's: the test
    database is the worker's own, and the application's moves with it only
    when it is the same database; any other, the placeholder of a run with
    no database among them, stays as it is."""
    if _same_database(database_url, test_db):
        database_url = _the_workers(database_url, worker)
    return _the_workers(test_db, worker), database_url


def _the_workers(url: str, worker: str) -> str:
    """The URL of `worker`'s database next to the run's, named after both.
    Postgres keeps 63 bytes of a name and cuts the rest without a word, so a
    run's name too long for its workers' would give two workers one
    database: refused instead."""
    u = make_url(url)
    name = f"{u.database}_{worker}"
    if len(name.encode()) > 63:
        raise pytest.UsageError(
            f"TEST_DB {u.database!r} is too long for its workers' databases "
            f"({name!r} is over Postgres's 63 bytes): name the run shorter")
    return u.set(database=name).render_as_string(hide_password=False)


def server_url(url: str = None) -> str:
    """The test server's maintenance database, `postgres`, where a database is
    made or dropped: a statement about a database never runs inside it, and
    this one exists on every server (initdb makes it)."""
    return make_url(url or TEST_DB).set(database=_MAINTENANCE_DATABASE).render_as_string(
        hide_password=False)


# The fixtures that reach a database: the test database (`own_database`, and
# `engine` on it, behind `db`), and the scratch databases the migration tests
# make for themselves.
# This mark alone decides the lane: no file marks itself whole (audit of
# 20 Oct 2026: a file's own mark kept its source-only guards out of `make test`;
# tests/test_the_unit_lane_runs_what_needs_no_database.py holds it).
_DATABASE_FIXTURES = {"own_database", "engine", "scratch", "scratch_db"}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    """A test that uses the test database is a `db` test, marked or not.

    Every way to it goes through the `engine` fixture below, so a test whose
    fixtures reach it is marked here, before `-m "not db"` deselects: a file
    that forgot its `pytestmark` (tests/test_user_scope.py, 25 tests) no
    longer turns `make test` red, and its tests of the rules alone still run
    there (audit of 17 Oct 2026)."""
    for item in items:
        if _DATABASE_FIXTURES & set(getattr(item, "fixturenames", ())) \
                and not item.get_closest_marker("db"):
            item.add_marker(pytest.mark.db)
        if not CLOUD and item.get_closest_marker("cloud"):
            item.add_marker(_NO_CLOUD)


def pytest_runtest_setup(item):
    """A test marked `serial` (pyproject.toml) needs the machine to itself,
    so in a worker of a parallel run it is an error, never a pass on luck
    and never a skip: `make test-all WORKERS=N` runs those after the parallel
    pass, in one process (`-m serial`), and a run by hand does the same,
    `-n N -m "not serial"` and then `-m serial`."""
    if WORKER and item.get_closest_marker("serial"):
        pytest.fail(f"{item.nodeid} is marked serial and this is worker {WORKER} of a "
                    "parallel run: run it in one process (-m serial), as "
                    "`make test-all WORKERS=N` does after the parallel pass", pytrace=False)


# The Cloud's package (korely_agent/hosted/) is optional (tests/cloud.py): a
# test that needs it is marked `cloud` (a test, a whole file with
# `pytestmark`, or a case with `pytest.param(..., marks=pytest.mark.cloud)`),
# and skips where the package is not, since KORELY_MODE=hosted is refused
# there (core/extensions.cloud).
from tests.cloud import HERE as CLOUD  # noqa: E402

_NO_CLOUD = pytest.mark.skip(reason="needs the Cloud's package (korely_agent/hosted/), "
                                    "which this tree does not carry")

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "testserver"}
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _host_of(address):
    if isinstance(address, tuple) and address:
        return address[0]
    return address


def _is_local(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode()
    # A unix socket is a path, not a host.
    return not isinstance(host, str) or host in _LOCAL_HOSTS or host.startswith("/")


@pytest.fixture(autouse=True)
def _no_external_network(request, monkeypatch):
    """Close the network to anything but this machine, unless the test is
    marked real_network (and then it runs only with RUN_REAL_NETWORK=1).

    A refused connection raises at once, so a code path that forgot to check
    for a provider fails loudly here instead of spending money and time on
    every run. Postgres is reached by libpq, below Python's socket module,
    and on localhost anyway; the webhook tests use local receivers."""
    if request.node.get_closest_marker("real_network"):
        if os.environ.get("RUN_REAL_NETWORK") != "1":
            pytest.skip("talks to a real external service: set RUN_REAL_NETWORK=1 to run it")
        yield
        return

    def guarded_connect(sock, address):
        if not _is_local(_host_of(address)):
            raise ConnectionRefusedError(
                f"hermetic tests: connection to {_host_of(address)!r} refused "
                f"(mark the test real_network)")
        return _real_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if not _is_local(_host_of(address)):
            raise ConnectionRefusedError(
                f"hermetic tests: connection to {_host_of(address)!r} refused "
                f"(mark the test real_network)")
        return _real_connect_ex(sock, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_local(host):
            raise socket.gaierror(
                f"hermetic tests: lookup of {host!r} refused (mark the test real_network)")
        return _real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield


# The names RFC 2606 keeps for documentation, and a public address for them.
# The webhook tests register endpoints on these, and a webhook URL is accepted
# only if its host resolves to a public address (services/webhooks.check_url),
# so those tests asked a real resolver: they passed or failed on the DNS of
# whatever machine ran them. The address is example.com's long-standing one;
# the point is only that it is global.
_DOCUMENTATION_NAMES = {name: "93.184.216.34"
                        for name in ("example.com", "example.org", "example.net")}


@pytest.fixture
def public_dns(monkeypatch):
    """The documentation names resolve to a public address, without a
    resolver; every other name still goes through the closed network."""
    closed = socket.getaddrinfo

    def resolve(host, port=None, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name in _DOCUMENTATION_NAMES:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                     (_DOCUMENTATION_NAMES[name], port or 0))]
        return closed(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


#: What `_no_session_setting_goes_back` found, until a test looks: (the
#: connection's pool entry, the names of the settings it carried back, which
#: test gave it back).
_SETTINGS_LEFT: list = []
#: The test running now (None between tests) and the one that ran last.
_NOW: dict = {"test": None, "last": None}


def _no_session_setting_goes_back(dbapi_connection, record) -> None:
    """`checkin` of every pool of the process (`Pool`, the class: the test
    engine's below, the product's own two, `db/base.get_engine` and
    `get_own_engine`, and any a test makes): a connection goes back with no
    setting of its own, as Postgres hands out a new one.

    A pool is the whole run's, and a session-level SET on one of its
    connections (not SET LOCAL, which ends with its transaction) stays with
    that connection for every test that takes it after. A test of one of the
    Cloud's parts (core/extensions.Part) set
    `idle_in_transaction_session_timeout` to 1.5 s on one of the test
    engine's and gave it back: in a whole run of 7 Nov 2026, an hour later, a
    test whose session waited 1.5 s in a transaction on that connection
    (while the trail rows of tests/test_requests_are_calls_on_the_memory.py
    were written, on a machine running two suites besides) lost it, and its
    request answered 503. Found here, the setting is reset at once, and the
    test that gave the connection back fails (`_no_setting_left_on_the_pool`)
    unless the pool closed it meanwhile (a NullPool, a pool over its size, a
    disposed engine): `Connection.detach()` before the SET keeps such a
    connection out of its pool (SQLAlchemy's own way to keep a changed
    connection from the rest of the program).

    What it reads is the session's own settings (`pg_settings.source` is
    `session`): an ALTER DATABASE or ALTER ROLE ... SET is the database's or
    the role's, for every connection made after it, and is not seen here
    (tests/test_audit_api.py moves the database's zone so and sets it back
    in its `finally`). Asked only of a connection `_may_set` marked, with
    one statement outside any transaction, after the pool's own reset (the
    rollback); Postgres only."""
    if record is not None and not record.info.pop(_MAY_HAVE_SET, False):
        return
    if dbapi_connection is None or type(dbapi_connection).__module__.split(".")[0] != "psycopg2":
        return
    try:
        autocommit = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
        try:
            cur = dbapi_connection.cursor()
            try:
                cur.execute("SELECT name FROM pg_settings WHERE source = 'session' ORDER BY name")
                names = [row[0] for row in cur.fetchall()]
                if names:
                    cur.execute("RESET ALL")
            finally:
                cur.close()
        finally:
            dbapi_connection.autocommit = autocommit
    except Exception:  # noqa: BLE001 - a connection that cannot answer is the pool's to drop
        return
    if names:
        _SETTINGS_LEFT.append((record, names, _NOW["test"] or f"after {_NOW['last']}, "
                                                             "between two tests"))


#: The mark of `_may_set` in a connection's `info` (kept with the DBAPI
#: connection, as its pool entry's).
_MAY_HAVE_SET = "tests: a statement with SET"


#: The pieces of a statement its words are read past, as PostgreSQL's lexical
#: structure has them (the documentation's 4.1): a line comment, the start
#: of a block comment (they nest: `_sets_a_session_setting` finds its end),
#: the tag of a dollar-quoted string (its end too), a string (with
#: backslashes when it is an E'' one), a quoted identifier, the end of a
#: statement, a word.
_LEX = re.compile(r"""
    (?P<line>--[^\n]*)
  | (?P<block>/\*)
  | (?P<dollar>\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$)
  | (?P<escaped>[Ee]'(?:[^'\\]|\\.|'')*')
  | (?P<string>'(?:[^']|'')*')
  | (?P<quoted>"(?:[^"]|"")*")
  | (?P<end>;)
  | (?P<word>[A-Za-z_][A-Za-z_0-9$]*)
""", re.VERBOSE | re.DOTALL)


def _sets_a_session_setting(statement: str) -> bool:
    """Whether `statement` may change a setting of its session: one of its
    statements is a SET that is not SET LOCAL (its first two words, past
    any comment), or a DO block (which may run one), or it calls
    set_config. A statement without the letters `set` cannot (SQL has no
    other way, but a function of the server that runs one, and there is
    none in the schema), and is not read further. Wide on purpose: a
    `set_config` inside a literal marks too, and is asked for nothing."""
    low = statement.lower()
    if "set" not in low:
        return False
    if "set_config" in low:
        return True
    one = ";" not in statement
    pos, words = 0, []
    while True:
        m = _LEX.search(statement, pos)
        if m is None:
            return False
        kind, pos = m.lastgroup, m.end()
        if kind == "block":
            depth = 1
            while depth:
                opening, closing = statement.find("/*", pos), statement.find("*/", pos)
                if closing < 0:
                    return False
                if 0 <= opening < closing:
                    depth, pos = depth + 1, opening + 2
                else:
                    depth, pos = depth - 1, closing + 2
        elif kind == "dollar":
            closing = statement.find(m.group(), pos)
            if closing < 0:
                return False
            pos = closing + len(m.group())
        elif kind == "end":
            words = []
        elif kind == "word" and len(words) < 2:
            words.append(m.group().lower())
            if words[0] == "do":
                return True
            if len(words) == 2:
                if words[0] == "set" and words[1] != "local":
                    return True
                if one:
                    return False


def _may_set(conn, _cursor, statement, _parameters, _context, _executemany) -> None:
    """`before_cursor_execute` of every engine: a statement that may change
    a setting of its session (`_sets_a_session_setting`) marks its
    connection, for `_no_session_setting_goes_back` to ask the server at
    the checkin; a connection that ran none is not asked (the checkin's
    question costs about a millisecond, a whole `pg_settings` built: 7 Nov
    2026). Not seen: a statement sent past SQLAlchemy, on the DBAPI's own
    cursor (only scripts/measure_* do), and a function of the server that
    sets one (none in the schema)."""
    if _sets_a_session_setting(statement):
        conn.connection.info[_MAY_HAVE_SET] = True


event.listen(Engine, "before_cursor_execute", _may_set)
event.listen(Pool, "checkin", _no_session_setting_goes_back)


def _left_on_a_pool() -> list:
    """What went back to a pool with a setting of its own and is still in
    it, by who gave it back; the list emptied."""
    left = [(names, who) for record, names, who in _SETTINGS_LEFT
            if record.dbapi_connection is not None]
    _SETTINGS_LEFT.clear()
    return left


def _fail_for(left: list, when: str) -> None:
    pytest.fail(f"{when}: a connection went back to its pool with a session setting of its "
                f"own, {left} (the setting, who gave it back): every test after it that takes "
                "that connection has it too. SET LOCAL inside the transaction, or "
                "Connection.detach() before the SET so the connection is closed instead of "
                "pooled", pytrace=False)


@pytest.fixture(autouse=True)
def _no_setting_left_on_the_pool(request):
    """The test that gave a connection back to a pool with a setting of its
    own fails at its teardown, naming the settings
    (`_no_session_setting_goes_back`). Set up before the test's own
    fixtures, so it ends after `db`, whose teardown gives connections back
    too. A fixture of a module, a class or the session gives its
    connections back between two tests: the next test fails at its set-up,
    naming the one before it, and after the last one
    `_no_setting_left_after_the_run` does."""
    left = _left_on_a_pool()
    _NOW["test"] = request.node.nodeid
    if left:
        _fail_for(left, "before this test")
    yield
    _NOW["test"], _NOW["last"] = None, request.node.nodeid
    left = _left_on_a_pool()
    if left:
        _fail_for(left, "this test")


@pytest.fixture(autouse=True, scope="session")
def _no_setting_left_after_the_run():
    """What the fixtures of the last module gave back with a setting of its
    own, after every test (`_no_setting_left_on_the_pool`)."""
    yield
    left = _left_on_a_pool()
    if left:
        _fail_for(left, "after the last test")


@pytest.fixture(scope="session")
def own_database():
    """The URL of this process's test database. One process: the run's,
    which `make test-db` makes. A worker of a parallel run: its own, made
    here when missing and dropped when the worker ends, so a run leaves no
    database behind but the one it was given.

    Before the drop the worker closes its own connections: the
    application's engines (db/base.py) keep theirs in their pools for the
    life of the process. It used to leave them to WITH (FORCE), which ends
    only the sessions of the database it drops (the Postgres docs, DROP
    DATABASE; tried on the test server, 6 Nov 2026: a session on another
    database lives on), but writes "terminating connection due to
    administrator command" in the server's log for each, the line a run
    beside it reads as its own connections cut. FORCE stays for a
    connection some test left open: it is this worker's, on this worker's
    database, and nothing waits on it any more; it comes after a drop
    without FORCE, which waits for the closed ones to leave
    (`drop_a_runs_database`)."""
    if not WORKER:
        yield TEST_DB
        return
    name = make_url(TEST_DB).database
    server = create_engine(server_url(), isolation_level="AUTOCOMMIT")
    quoted = server.dialect.identifier_preparer.quote_identifier(name)
    try:
        with server.connect() as c:
            if not c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"),
                             {"n": name}).scalar():
                c.execute(text(f"CREATE DATABASE {quoted}"))
        yield TEST_DB
        drop_a_runs_database(server, name)
    finally:
        server.dispose()


#: SQLSTATE object_in_use (PostgreSQL, Appendix A): DROP DATABASE without
#: FORCE refused, a session stayed in the database.
_OBJECT_IN_USE = "55006"


def drop_a_runs_database(server, name: str) -> int:
    """Drop `name`, a database of this run's own (a worker's), after closing
    the connections this process's db/base.py engines keep, and say how many
    sessions were still on it, the ones WITH (FORCE) then ended: an engine a
    test left open, or a worker stopped halfway, both this run's.

    A connection the pool has closed is not gone from the server when the
    close returns: its backend leaves the database when its process ends, a
    moment later, and later still on a machine with few processors. Counted
    at once, one of them was taken for a session left open on the CI's runs
    of 10 Nov 2026 (2 for 1), and in 4 runs of 15 with the test and its
    Postgres on two processors, as on that runner (11 Nov 2026); WITH
    (FORCE) would end such a backend too, with the "terminating connection"
    line in the server's log that the closing is there to avoid. So the
    database is dropped first without FORCE, which waits for the backends
    still in it, at most 5 seconds (PostgreSQL 17, procarray.c,
    CountOtherDBBackends: "normal user backends are just waited for"), and
    refuses with object_in_use only when one stays. Then the clients'
    sessions are counted, and FORCE ends them; not the other processes in
    the database: an autovacuum worker that came in during the wait (3 runs
    of 10 on two processors), which the drop ends in any case, or the
    parallel worker of a session's query, which is that session's."""
    import sys

    from sqlalchemy.exc import OperationalError

    base = sys.modules.get("korely_agent.db.base")
    if base is not None:
        for cached in (base.get_engine, base.get_own_engine):
            if cached.cache_info().currsize:
                cached().dispose()
    quoted = server.dialect.identifier_preparer.quote_identifier(name)
    with server.connect() as c:
        try:
            c.execute(text(f"DROP DATABASE IF EXISTS {quoted}"))
            return 0
        except OperationalError as e:
            orig = getattr(e, "orig", None)
            if (getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)) != _OBJECT_IN_USE:
                raise
        still = c.execute(text("SELECT count(*) FROM pg_stat_activity "
                               "WHERE datname = :n AND backend_type = 'client backend'"),
                          {"n": name}).scalar()
        c.execute(text(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)"))
    return still


def _check_migration():
    """scripts/check_migration.py, which holds the rule for a database that
    may be emptied and the one way to empty it (scripts/ is no package)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "check_migration.py"
    spec = importlib.util.spec_from_file_location("check_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def engine(own_database):
    """The run's database, emptied whole and given this tree's schema.

    Whole, not this tree's tables: a drop of the tables the models declare
    stopped on whatever else the database held with a foreign key to one of
    them, a table that left the models (`agents` and `agent_daily_usage`,
    6 Oct 2026) or one another branch had left in the shared korely_test
    (3,438 errors in the release's serial pass, 13 Nov 2026). So the schema goes, as
    scripts/check_migration.py --wipe does it, and only on a database whose
    name says `test`, that script's rule."""
    eng = create_engine(own_database, future=True)
    check = _check_migration()
    why = check.refusal(own_database, make_url(own_database).database)
    if why:
        raise pytest.UsageError(f"the suite empties the database it is given: {why}")
    with eng.begin() as c:
        check.empty_the_schema(c)
    # From the package and not from db.base: the package imports every model,
    # which is what puts the tables on this metadata.
    from korely_agent.db import Base

    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """A real session, with the tables emptied afterwards.

    The obvious fixture wraps each test in a transaction and rolls it back, and
    it is wrong here. The service layer commits: `write_fact_direct` persists,
    runs contradiction detection against what is now durable, and commits again.
    Under an outer transaction that sequence executes differently from the way
    it executes in production, so the fixture would be testing a program we do
    not ship. Truncating is slower and tests the real thing.
    """
    from korely_agent.db.base import Base

    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = Session()
    try:
        yield s
    finally:
        # Roll back anything the test left open before closing: a session with a
        # live transaction still holds locks, and the TRUNCATE below would then
        # block or silently do nothing, leaking rows into the next test.
        s.rollback()
        s.close()
        with engine.begin() as c:
            names = ", ".join(f'"{t}"' for t in reversed(Base.metadata.sorted_tables))
            c.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def tenant(db):
    from korely_agent.db.models import Tenant

    t = Tenant(id=uuid.uuid4(), name="Acme Insurance")
    db.add(t)
    # Committed, not flushed. The audit trail writes in its own session by
    # design, and a session that is not this one cannot see an uncommitted row:
    # every audit insert would fail its foreign key and be swallowed, which is
    # a test suite that passes while the feature does nothing.
    db.commit()
    return t


@pytest.fixture(autouse=True)
def _fresh_settings_and_no_worker(monkeypatch):
    """Every test starts from settings read now, with the worker off.

    `get_settings()` is cached for the process and `create_app` reads it. The
    fixtures set RUN_WORKER=false and then built a TestClient, which is right
    only while nothing has cached the settings yet: once an earlier test had,
    its `run_worker=True` won, and the worker started with the client. Its
    first drain then raced the rows the test had just seeded (49 tests did,
    counted 2026-09-28; tests/test_the_worker_stays_off.py).

    So the environment says no worker and the cache is emptied, before and
    after each test, and nothing a test sets leaks into the next one. A test
    that wants the worker sets RUN_WORKER=true and clears the cache itself.
    Reading the settings needs DATABASE_URL, and a run without a database
    has the placeholder set at the top of this file.
    """
    from korely_agent.config import get_settings

    monkeypatch.setenv("RUN_WORKER", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _a_part_of_the_cloud_runs_in_its_mode(request, monkeypatch, _fresh_settings_and_no_worker):
    """A test of a part of the product the Cloud's package carries
    (core/extensions.Part) runs as the Cloud, the one mode that serves it
    (core/extensions.mounted): KORELY_MODE=hosted, with the sign-in's
    project the tests' (tests/hosted_helpers.PROJECT). Which tests are a
    part's: the file names tests/hosted_parts.HOSTED_MODE_TESTS gives, one
    setting for all of them; none in the public repository."""
    import fnmatch

    from tests import cloud

    names = cloud.of_the_parts("HOSTED_MODE_TESTS", tuple)
    if not any(fnmatch.fnmatch(request.node.path.name, n) for n in names):
        return
    from korely_agent.config import get_settings

    monkeypatch.setenv("KORELY_MODE", "hosted")
    get_settings.cache_clear()
    # The part's own set-up for its tests (its settings, an account's plan,
    # its sign-in): tests/hosted_parts.HOSTED_MODE_SETUP.
    for set_up in cloud.of_the_parts("HOSTED_MODE_SETUP", tuple):
        set_up(monkeypatch)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_round_outlives_its_test():
    """A worker's round leaves an attempt that runs long to end in its own
    thread and write its own outcome (core/threads.Lanes): the next test's
    tables must not receive it. Only when the modules are loaded."""
    import sys

    yield
    from tests import cloud

    for name, attr in (("korely_agent.services.webhooks", "_sending"),
                       ("korely_agent.services.facts.drain", "_draining"),
                       # A part of the product's own (core/extensions.Part).
                       *cloud.of_the_parts("POOLS_IDLE_AFTER_A_TEST", tuple)):
        module = sys.modules.get(name)
        lanes = getattr(module, attr, None) if module is not None else None
        if lanes is not None:
            from tests.thread_helpers import wait_idle

            wait_idle(lanes, 30)


@pytest.fixture(autouse=True)
def _a_fresh_holder_for_every_test():
    """Every test starts with the facts drain's holder of a process just
    started (services/holder.py): no key, nothing given back. The holder is
    one per process, and a test that stops an app with the worker on gives
    its claims back for good, so every later test of the process claimed
    nothing; and its own connection, out of every pool, would outlive the
    engines the suite disposes and count as a session the database's drop
    had to end. Only when the module is loaded."""
    import sys

    def reset():
        drain = sys.modules.get("korely_agent.services.facts.drain")
        if drain is None:
            return
        old, drain._holder = drain._holder, type(drain._holder)()
        old.give_back(lambda conn, key: 0)

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def _the_applications_engine_is_the_runs():
    """No test hands the next one an engine on another database.

    The application builds its engines once per process, on the DATABASE_URL
    of that moment (db/base.py, `get_engine`, `get_own_engine` and their
    sessionmakers). A test that builds the app with DATABASE_URL on a server
    that is not there (tests/test_blind_install.py, the /health of an install
    whose database is down) left them on that server, and every later test of
    the process that reached the database through the app was refused: seven
    of tests/test_recompute_vectors.py right after it, 54 tests in all in a
    run in four processes. The suite in one process passed only because a
    test earlier in the alphabet had built them on the test database first
    (found when the suite first ran in several processes, 6 Nov 2026). So
    before and after each test, engines on any database but the run's are
    disposed and forgotten; the run's are kept, and their pools with them.
    Only when the module is loaded, as for the MCP server below."""
    import sys

    def reset():
        base = sys.modules.get("korely_agent.db.base")
        if base is None or _RUN_DATABASE_URL is None:
            return
        for cached in (base.get_engine, base.get_own_engine,
                       base.get_sessionmaker, base.get_own_sessionmaker):
            if not cached.cache_info().currsize:
                continue
            made = cached()
            engine = made.kw["bind"] if hasattr(made, "kw") else made   # a sessionmaker's own
            if not _same_database(engine.url, _RUN_DATABASE_URL):
                engine.dispose()
                cached.cache_clear()

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def _a_fresh_mcp_server_for_every_test():
    """Every test that builds the app gets an MCP transport that starts.

    The MCP server is one instance per process (mcp/server.py), which is
    right for the one app a process serves. Its session manager runs once per
    instance ("StreamableHTTPSessionManager .run() can only be called once
    per instance. Create a new instance if you need to run again", the
    library's own words), so the first TestClient of the suite started it and
    every later one logged "MCP session manager failed to start" and ran with
    no MCP transport at all: a request to /mcp in those tests would have
    failed, and nothing looked at it. A handful of fixtures reset the two
    singletons by hand; this does it for every test, before and after, so
    the next `create_app` builds a new instance, the way the library asks.
    Only when the module is loaded: importing it is not this fixture's job.
    """
    import sys

    def reset():
        server = sys.modules.get("korely_agent.mcp.server")
        if server is not None:
            server._agent_mcp_singleton = None
            server._agent_streamable_app_singleton = None

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def _no_identity_from_the_test_before():
    """Every test starts and ends with no caller in the context variables.

    The MCP's identity (the tenant, the project, the scopes, the key, the end
    users a key may name; mcp/server.py), the region of a write in progress
    (core/llm.py), the model-usage scope (services/llm_usage.py), the OAuth
    caller's network (services/oauth.py) and the request's scope (main.py)
    live in ContextVars, and pytest runs every test in one context: a test
    that set the tenant outside a task of its own (tests/test_memory_engine.py
    did) handed it to every test after it (audit of 17 Oct 2026). Each one's
    default is None. Only when the module is loaded, as for the MCP server
    above."""
    import sys

    names = {
        "korely_agent.mcp.server": ("_agent_user_id", "_agent_project", "_agent_scopes",
                                    "_agent_key_id", "_agent_end_users"),
        "korely_agent.core.llm": ("_region",),
        "korely_agent.services.llm_usage": ("_active",),
        "korely_agent.services.oauth": ("NETWORK",),
        "korely_agent.main": ("_REQUEST",),
    }

    def reset():
        for module, variables in names.items():
            loaded = sys.modules.get(module)
            if loaded is not None:
                for name in variables:
                    var = getattr(loaded, name)
                    if var.get() is not None:
                        var.set(None)

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def _the_minute_holds(monkeypatch):
    """The rate limiter's clock stands still for the test, half way through
    the minute it starts in.

    A test that counts calls against RATE_LIMIT_PER_MINUTE (or a hosted
    plan's minute, hour or day) and sees a 429 on the third failed whenever
    the minute turned between its calls: the counter started again at one
    (tests/test_mcp_scopes.py, audit of 17 Oct 2026). Held, not slept on: the
    windows are still the limiter's own buckets of that instant."""
    from datetime import datetime, timezone

    from korely_agent.services import rate_limit

    held = datetime.now(timezone.utc).replace(second=30, microsecond=0)
    monkeypatch.setattr(rate_limit, "clock", lambda: held)
    yield held


@pytest.fixture(autouse=True)
def _the_core_as_it_ships():
    """Every test starts and ends with no hook of the hosted service.

    `hosted.install` registers its hooks in the core (core/extensions.py) and
    its account events in the webhooks (`quota.warning`), and they are the
    process's, not the app's: only the next `create_app` took them away. A
    test that built a hosted app last (tests/test_hosted_mode.py) left the
    Cloud's spending brake on every model call after it until some test built
    an app again, so tests/test_llm.py run right after it failed five tests
    on "no price for mistral-small-latest in LLM_RATES" (audit of 17 Oct
    2026). The whole suite was green only by the alphabetical order of its
    files. Only when the modules are loaded, as for the MCP server above."""
    import sys

    def reset():
        extensions = sys.modules.get("korely_agent.core.extensions")
        if extensions is not None:
            extensions.reset()
        webhooks = sys.modules.get("korely_agent.services.webhooks")
        if webhooks is not None:
            webhooks.reset_events()

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def _the_thresholds_start_from_the_default():
    """memory_core keeps, for the process, which embedding model makes the
    vectors (`thresholds.use_model`, 2.4.0): `create_app` sets it, and so do the
    tests of the thresholds. A test must not hand its model to the next one."""
    from korely_agent.services.memory_core import thresholds

    yield
    thresholds.use_model("gemini-embedding-2")


@pytest.fixture(autouse=True)
def _no_provider_leaks_between_tests():
    """Drop the memoized provider around every test.

    `get_provider()` caches, deliberately: constructing a client per write would
    pay for a TLS handshake on the write path. The cache outlives the
    environment variable that filled it, so one test setting GOOGLE_API_KEY to a
    placeholder left a real Gemini client installed for every test that ran
    after it, and those tests then made actual calls to Google with an invalid
    key.

    Nobody noticed because the two calls that reached it swallowed their
    exceptions. The moment they stopped swallowing, eight unrelated tests turned
    red at setup and the suite showed what it had been doing all along. A test
    suite that reaches the network is not slow, it is untrustworthy: it passes
    or fails on somebody else's uptime.
    """
    from korely_agent.core import llm

    llm.reset_provider_cache()
    yield
    llm.reset_provider_cache()


# What a process remembers of its own work, which /health reports and the
# drain decides on: the model's and the vectors' last answers, the local
# embedder's, the worker's jobs and its export, the drain's rests after a
# refusal (with their lock), and the bookkeeping's ledgers below.
_PROCESS_RECORDS = (
    ("korely_agent.core.llm", "_last_call", None),
    ("korely_agent.core.llm", "_last_embedding", None),
    ("korely_agent.core.local_embedder", "_last", None),
    ("korely_agent.core.local_embedder", "_warming", None),
    ("korely_agent.worker", "_job_state", None),
    ("korely_agent.worker", "_export_state", None),
    ("korely_agent.services.facts.drain", "_pauses", "_pause_lock"),
)
_AS_A_NEW_PROCESS_HAS_THEM = {}


@pytest.fixture(autouse=True)
def _every_test_starts_with_the_records_of_a_new_process():
    """No test reads what an earlier one left in the process's records
    (`_PROCESS_RECORDS` and the bookkeeping's ledgers; not the others a
    process keeps, the Cloud's spending alerts and its cache, the exports
    still owed).

    tests/test_writer_refused_for_payment.py and tests/test_drains.py leave
    the model's last answer failing (a 402, a 504) in core/llm, and the
    /health of tests/test_hosted_writers_hostile.py run after them in the
    same process said "degraded" with both writers saved: two failures in a
    run in four processes (6 Nov 2026). That /health keeps a failure from
    before new writers were saved is the product's, fixed apart; here no
    test starts from what the one before left, as a dozen tests did by hand
    for the records they read. Before each test (after the one before has
    ended its rounds, `_no_round_outlives_its_test`)."""
    as_a_new_process()
    yield


def as_a_new_process():
    """Every record of `_PROCESS_RECORDS` as a new process has it, copied
    once, at the first call, which is the first test's, before any test has
    run; the ledgers by their own `reset`."""
    import contextlib
    import importlib

    from korely_agent.services import bookkeeping

    for name, attr, lock in _PROCESS_RECORDS:
        module = importlib.import_module(name)
        record = getattr(module, attr)
        fresh = _AS_A_NEW_PROCESS_HAS_THEM.setdefault((name, attr), dict(record))
        with getattr(module, lock) if lock else contextlib.nullcontext():
            record.clear()
            record.update(fresh)
    for ledger in vars(bookkeeping).values():
        if isinstance(ledger, bookkeeping.Ledger):
            ledger.reset()


def seeded_project_id(db, tenant_id):
    """The project a row seeded straight into the database belongs to.

    Since keys are bound to the account's default project when they are made,
    a row put in the database with no project is a row no key can see. That is
    correct behaviour and it makes the obvious fixture wrong: seeding with no
    project models an install that cannot exist any more.

    So a fixture that writes rows by hand asks for this and passes it, which is
    what a write through the API would have stamped anyway.
    """
    from korely_agent.services import projects as project_service

    return project_service.get_or_create_default_project(db, tenant_id).id


def stub_extraction(monkeypatch, module, facts, observation=None):
    """Answer the write's extraction the way `facts`, a stand-in for the
    model's facts, would: called with the memory's text and the same
    keywords, returning its facts or raising. Since memory_core 2.6.0 the
    write asks for the facts and the observation in one call
    (`extract_facts_and_observation`); `observation` is what that call gives
    besides, None when the model gave no such field."""
    from korely_agent.services.memory_core.fact_extractor import Extraction

    def extraction(content, **kw):
        kw.pop("previous", None)
        kw.pop("date", None)
        return Extraction(list(facts(content, **kw)), observation)

    monkeypatch.setattr(module, "extract_facts_and_observation", extraction)


# The contradiction judge as a scripted model meets it (2026-10-07). One fact is
# asked with "EXISTING facts:" and answers {"contradictions": [...]}; a write
# judged in one call (`_BATCHED_JUDGE`, on by default as on the hosted product)
# is asked in blocks "[new_id=...] NEW: ... candidates:" and answers
# {"results": [{"new_id": ..., "contradictions": [...]}]}, every new_id present.
_NEW_ID = re.compile(r"\[new_id=([^\]]+)\] NEW: ")


def is_the_judge(prompt: str) -> bool:
    return "EXISTING facts:" in prompt or "[new_id=" in prompt


def judge_reply(prompt: str, contradictions_for) -> str:
    """The judge's answer in the shape `prompt` asks. `contradictions_for(text)`
    gets the candidates' lines of one new fact and returns its list of
    {"existing_id", "reason"}."""
    if "[new_id=" not in prompt:
        existing = prompt.split("EXISTING facts:", 1)[1].split("NEW fact", 1)[0]
        return json.dumps({"contradictions": contradictions_for(existing)})
    parts = _NEW_ID.split(prompt)
    results = [{"new_id": parts[i], "contradictions": contradictions_for(parts[i + 1])}
               for i in range(1, len(parts) - 1, 2)]
    return json.dumps({"results": results})


def in_the_judges_shape(prompt: str, answer):
    """A one-fact verdict, `{"contradictions": [...]}`, given to every new fact
    of a batch, which keeps only the ids among its own candidates. Anything
    that is not such a verdict (an exception, an empty or broken answer)
    passes as it is: a failure stays a failure in both shapes."""
    if "[new_id=" not in prompt or not isinstance(answer, str):
        return answer
    try:
        verdict = json.loads(answer)
    except ValueError:
        return answer
    if not isinstance(verdict, dict) or not isinstance(verdict.get("contradictions"), list):
        return answer
    return judge_reply(prompt, lambda _text: verdict["contradictions"])
