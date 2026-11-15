"""The release's suite runs on a database of its own, and the suite starts on
an empty one whatever it held.

scripts/release.sh ran `make test-all WORKERS=4` with no TEST_DB, so the
Makefile's default, korely_test, the database every checkout on the machine
shares. The parallel pass runs on the workers' own databases, the serial
pass on TEST_DB itself, and there another branch had left a table with a
foreign key to `agent_projects`: the drop of this tree's tables
stopped on it and 3,438 tests ended in error (13 Nov 2026). Two causes, two
cures: the release names its database and drops it at its end, and the
suite empties the whole schema of the database it is given, as
scripts/check_migration.py --wipe does, instead of dropping the tables it
knows.

The release script runs in a folder of its own with a make, a docker and a
git that only write down what they were asked, as
tests/test_a_release_carries_its_own_heading.py runs its first lines.
"""
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from tests.conftest import server_url
from tests.test_the_test_database_is_named_by_the_run import _the_name_rule

ROOT = Path(__file__).resolve().parents[1]

GIT = """#!/bin/sh
case "$1" in
  rev-parse) exit 1 ;;   # the tag does not exist yet
  status) exit 0 ;;      # a clean working tree
esac
exit 0
"""

# Every call written down with the TEST_DB it was given; `test-all` exits
# with what the test asks for.
MAKE = """#!/bin/sh
for a in "$@"; do case "$a" in -*) ;; *) echo "$a TEST_DB=${TEST_DB:-}" >> "$CALLS" ;; esac; done
for a in "$@"; do [ "$a" = test-all ] && exit "${SUITE_EXIT:-0}"; done
exit 0
"""


def _release(tmp_path, suite_exit=0, test_db=None):
    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "release.sh", work / "scripts" / "release.sh")
    # Past the suite the script asks the image its version with a script
    # this folder does not have: it stops there, as after any failed step.
    (work / "scripts" / "stamp_installer.py").write_text("")
    (work / "install.sh").write_text('PUBLIC_REPO="example/example"\n')
    (work / "VERSION").write_text("9.9.8-dev\n")
    (work / "pyproject.toml").write_text('version = "9.9.8-dev"\n')
    (work / "CHANGELOG.md").write_text("Releases, newest first.\n\n## 9.9.9\n\n- A change.\n")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, body in (("git", GIT), ("make", MAKE), ("docker", "#!/bin/sh\nexit 0\n")):
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    calls = tmp_path / "calls"
    env = {k: v for k, v in os.environ.items() if k != "TEST_DB"}
    env.update(PATH=f"{bin_}{os.pathsep}{os.environ['PATH']}", CALLS=str(calls),
               SUITE_EXIT=str(suite_exit))
    if test_db is not None:
        env["TEST_DB"] = test_db
    r = subprocess.run(["bash", str(work / "scripts" / "release.sh"), "9.9.9"],
                       cwd=work, env=env, capture_output=True, text=True, timeout=60)
    made = [line.split(" TEST_DB=") for line in calls.read_text().splitlines()]
    return r, made


def _database_of(made, target):
    named = [db for t, db in made if t == target]
    assert len(named) == 1, made
    return named[0]


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash here")
@pytest.mark.parametrize("given", [None, "korely_test"], ids=["none in the shell", "the shared one in the shell"])
def test_the_suite_runs_on_a_database_the_release_names_and_drops(tmp_path, given):
    r, made = _release(tmp_path, test_db=given)
    suite = _database_of(made, "test-all")
    assert suite != "korely_test" and suite != (given or ""), made
    assert _the_name_rule().fullmatch(suite), f"{suite!r} passes the Makefile's test-db"
    assert not suite.endswith(("_gw0", "_gw1")), suite
    assert _database_of(made, "test-db-drop") == suite
    assert [t for t, _ in made].index("test-db-drop") > [t for t, _ in made].index("test-all")
    assert r.returncode != 0, "this folder stops the script after the suite"


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash here")
def test_a_failed_suite_drops_its_database_too_and_fails_the_release(tmp_path):
    r, made = _release(tmp_path, suite_exit=3)
    assert _database_of(made, "test-db-drop") == _database_of(made, "test-all")
    assert r.returncode == 3, r.stderr[-1000:]
    assert (tmp_path / "repo" / "VERSION").read_text() == "9.9.9\n", "no tag, nothing after the suite"


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash here")
def test_two_releases_on_one_machine_name_two_databases(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _, one = _release(tmp_path / "a")
    _, two = _release(tmp_path / "b")
    assert _database_of(one, "test-all") != _database_of(two, "test-all")


_STARTS_CLEAN = '''
from sqlalchemy import inspect


def test_the_run_starts_on_this_trees_tables_alone(engine):
    from korely_agent.db import Base

    assert set(inspect(engine).get_table_names()) == set(Base.metadata.tables)
'''


def test_the_suite_starts_clean_on_a_database_another_branch_left_a_table_in(own_database, tmp_path):
    """A table another branch left in korely_test, with its foreign key to
    `agent_projects`, in a database of this test's own; then a run of the
    suite's fixtures on it, in a process of its own."""
    from korely_agent.db import Base

    name = f"korely_test_lt_{uuid.uuid4().hex[:8]}"
    url = make_url(own_database).set(database=name).render_as_string(hide_password=False)
    admin = create_engine(server_url(own_database), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        left = create_engine(url)
        try:
            with left.begin() as c:
                c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            Base.metadata.create_all(bind=left)
            with left.begin() as c:
                c.execute(text("CREATE TABLE agent_rows_of_another_branch ("
                               "id uuid PRIMARY KEY, "
                               "project_id uuid NOT NULL REFERENCES agent_projects(id))"))
        finally:
            left.dispose()
        (tmp_path / "test_inner.py").write_text(_STARTS_CLEAN)
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(ROOT / "pyproject.toml"),
             "-p", "tests.conftest", str(tmp_path / "test_inner.py")],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
            env={**os.environ, "TEST_DATABASE_URL": url, "DATABASE_URL": url, "PYTHONPATH": str(ROOT)})
        assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
        check = create_engine(url)
        try:
            assert "agent_rows_of_another_branch" not in inspect(check).get_table_names()
        finally:
            check.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_the_suite_empties_only_a_database_whose_name_says_test(own_database, tmp_path):
    """The whole schema goes, so the rule of scripts/check_migration.py
    --wipe holds: a name that does not say `test` stops the run before any
    statement."""
    url = make_url(own_database).set(database="korely_production_lookalike").render_as_string(
        hide_password=False)
    (tmp_path / "test_inner.py").write_text(_STARTS_CLEAN)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(ROOT / "pyproject.toml"),
         "-p", "tests.conftest", str(tmp_path / "test_inner.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
        env={**os.environ, "TEST_DATABASE_URL": url, "DATABASE_URL": url, "PYTHONPATH": str(ROOT)})
    assert r.returncode != 0
    assert "is not a test database" in r.stdout + r.stderr, r.stdout[-2000:]
