"""The guard of tests/conftest.py that no connection goes back to a pool
with a session setting of its own (`_no_session_setting_goes_back`, after a
whole run of 7 Nov 2026 where a 1.5 s `idle_in_transaction_session_timeout`
left on a pooled connection by one test cost another its connection an hour
later): its own run, in a pytest of its own with the suite's conftest, on
files written here. Each way SQL has of setting a session's value fails the
test that gave the connection back, naming the setting and the test; the
connection reaches the next test with the setting taken back; what changes
nothing passes and is not asked. Without the guard every one of them
passes, and this test fails.
"""
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from tests import test_migrations

# The nested run's database: the migration tests' scratch one.
scratch = test_migrations.scratch

ROOT = Path(__file__).resolve().parent.parent

#: (test, the statement it runs on a pool of its own, the setting it leaves).
WAYS = (
    ("test_a_set", "SET statement_timeout = '4321ms'", "statement_timeout"),
    ("test_in_lower_case", "set random_page_cost = 4.321", "random_page_cost"),
    ("test_by_set_config", "SELECT set_config('cpu_tuple_cost', '0.04321', false)",
     "cpu_tuple_cost"),
    ("test_after_another_statement", "SELECT 1; SET seq_page_cost = 4.321", "seq_page_cost"),
    ("test_after_a_line_comment", "-- why\nSET enable_seqscan = off", "enable_seqscan"),
    ("test_after_a_block_comment", "/* why /* and why */ */ SET enable_hashjoin = off",
     "enable_hashjoin"),
    ("test_in_a_do_block", "DO $$ BEGIN EXECUTE 'SET enable_mergejoin = off'; END $$",
     "enable_mergejoin"),
)

_FIRST = '''
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from tests.conftest import _MAY_HAVE_SET

URL = os.environ["TEST_DATABASE_URL"]
#: One connection: the test after the one that gave it back takes it again.
ONE = create_engine(URL, pool_size=1, max_overflow=0)


def _set(engine, sql):
    with engine.connect() as c:
        c.exec_driver_sql(sql)
        c.commit()


@pytest.mark.parametrize("sql", [%(ways)s], ids=[%(ids)s])
def test_way(sql):
    _set(create_engine(URL), sql)


def test_the_products_engine():
    from korely_agent.db import get_engine

    _set(get_engine(), "SET lock_timeout = '4322ms'")


def test_gives_it_back_on_a_pool_of_one():
    _set(ONE, "SET effective_cache_size = '4329MB'")


def test_takes_it_again_reset():
    with ONE.connect() as c:
        got = c.exec_driver_sql(
            "SELECT source FROM pg_settings WHERE name = 'effective_cache_size'").scalar()
    assert got != "session", got


def test_set_local_is_not_asked():
    with ONE.connect() as c:
        c.exec_driver_sql("SET LOCAL statement_timeout = '4323ms'; "
                          "SET LOCAL lock_timeout = '4323ms'")
        assert not c.connection.info.get(_MAY_HAVE_SET)
        c.commit()


def test_a_pool_that_closes_it():
    _set(create_engine(URL, poolclass=NullPool), "SET statement_timeout = '4324ms'")


def test_detached():
    with create_engine(URL).connect() as c:
        c.detach()
        c.exec_driver_sql("SET statement_timeout = '4325ms'")
        c.commit()


@pytest.fixture(scope="module")
def held():
    c = create_engine(URL).connect()
    c.exec_driver_sql("SET work_mem = '4326kB'")
    c.commit()
    yield
    c.close()


def test_with_a_fixture_of_the_module(held):
    pass
'''

_SECOND = '''
def test_the_next_one():
    pass
'''

_LAST = '''
import os

import pytest
from sqlalchemy import create_engine


@pytest.fixture(scope="module")
def held():
    c = create_engine(os.environ["TEST_DATABASE_URL"]).connect()
    c.exec_driver_sql("SET maintenance_work_mem = '4327kB'")
    c.commit()
    yield
    c.close()


def test_the_last_one(held):
    pass
'''


def _outcomes(xml: Path) -> dict:
    """{test name: (outcome, its text)} from the run's JUnit report: `passed`,
    or `error` / `failure` with pytest's message."""
    out = {}
    for case in ET.parse(xml).getroot().iter("testcase"):
        bad = [(child.tag, (child.get("message") or "") + (child.text or ""))
               for child in case if child.tag in ("error", "failure")]
        out[case.get("name")] = (bad[0][0], " ".join(b[1] for b in bad)) if bad else ("passed", "")
    return out


def test_every_setting_given_back_to_a_pool_fails_the_test_that_gave_it(scratch, tmp_path):
    """The nested run is a run of its own, on a database of its own: the
    migration tests' scratch database, a name of its own made and dropped
    around it. It ran on this process's test database, which in a worker of
    a parallel run is the worker's (<run>_gw<n>), a name the suite refuses
    for a run of its own (conftest.pytest_configure: a parallel run of the
    shorter name would take it as its worker's and drop it), so in `make
    test-all WORKERS=N` the nested run stopped before its first test and
    this one failed (the merge of main's parallel suite, 7 Nov 2026)."""
    first = _FIRST % {"ways": ", ".join(repr(sql) for _name, sql, _setting in WAYS),
                      "ids": ", ".join(repr(name) for name, _sql, _setting in WAYS)}
    files = []
    for name, text in (("test_first.py", first), ("test_second.py", _SECOND),
                       ("test_last.py", _LAST)):
        (tmp_path / name).write_text(text)
        files.append(str(tmp_path / name))
    xml = tmp_path / "run.xml"
    url, _engine = scratch
    env = {**os.environ, "PYTHONPATH": str(ROOT), "TEST_DATABASE_URL": url, "DATABASE_URL": url}
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                          "-p", "tests.conftest", "--rootdir", str(tmp_path),
                          f"--junitxml={xml}", *files],
                         cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    assert xml.exists(), run.stdout[-3000:] + run.stderr[-3000:]
    got = _outcomes(xml)
    # The test that gave it back, by the setting it left, named in its own
    # failure.
    gave = {f"test_way[{name}]": setting for name, _sql, setting in WAYS}
    gave.update({"test_the_products_engine": "lock_timeout",
                 "test_gives_it_back_on_a_pool_of_one": "effective_cache_size"})
    for name, setting in gave.items():
        outcome, text = got.get(name, ("missing", ""))
        assert outcome == "error" and f"['{setting}']" in text \
            and f"test_first.py::{name}'" in text, (name, outcome, text[-1500:])
    # A fixture of a module gave it back between two tests: the next test
    # fails, naming the one before it; after the last test, the run fails.
    outcome, text = got.get("test_the_next_one", ("missing", ""))
    assert outcome == "error" and "['work_mem']" in text \
        and "test_with_a_fixture_of_the_module" in text, (outcome, text[-1500:])
    outcome, text = got.get("test_the_last_one", ("missing", ""))
    assert outcome == "error" and "['maintenance_work_mem']" in text, (outcome, text[-1500:])
    # The connection given back reaches the next test with its setting taken
    # back; SET LOCAL marks nothing; a connection closed or detached is not
    # one a test after takes.
    for name in ("test_takes_it_again_reset", "test_set_local_is_not_asked",
                 "test_a_pool_that_closes_it", "test_detached", "test_with_a_fixture_of_the_module"):
        assert got.get(name, ("missing", ""))[0] == "passed", (name, got.get(name))
