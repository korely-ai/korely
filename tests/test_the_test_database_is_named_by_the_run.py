"""The suite empties the whole schema of the database it is given and makes
its tables again (tests/conftest.py, `engine`), and every checkout on a machine (worktrees, other sessions) shares the one test
Postgres on 55433. With the name written into the Makefile, a release run and
another session's run on the same machine would empty each other's tables
halfway through (seen on 4 Nov 2026, before the release of 1.0.8, with seven
sessions' worktrees on one Postgres). So the Makefile reads the name from
TEST_DB, which a run sets on make's command line or in the environment, and
creates that database when it is missing."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = (ROOT / "Makefile").read_text()


def _recipe(target: str) -> str:
    """The lines of one target, up to the next blank line."""
    m = re.search(rf"^{re.escape(target)}:.*?(?=\n\n|\Z)", MAKEFILE, re.M | re.S)
    assert m, f"no {target} target in the Makefile"
    return m.group(0)


def test_the_default_is_the_database_the_compose_file_makes():
    compose = (ROOT / "dev" / "docker-compose.test.yml").read_text()
    assert "POSTGRES_DB: korely_test" in compose
    assert re.search(r"^TEST_DB \?= korely_test$", MAKEFILE, re.M), \
        "TEST_DB is set with ?=, so the environment's value wins"
    assert "TEST_DB_URL = postgresql://korely:test@localhost:55433/$(TEST_DB)" in MAKEFILE


@pytest.mark.parametrize("target", ["test-all", "migrate-check"])
def test_every_target_on_the_database_reads_its_name(target):
    recipe = _recipe(target)
    assert recipe.split("\n", 1)[0].endswith(" test-db"), \
        f"{target} depends on test-db, which makes the database it names"
    assert "korely_test" not in recipe, f"{target} writes a database name of its own"
    assert "$(TEST_DB_URL)" in recipe


def test_no_other_line_names_the_test_database():
    """The name is written once, on the TEST_DB line: a second copy is a run
    that ignores the one it was given."""
    lines = [ln for ln in MAKEFILE.splitlines()
             if "korely_test" in ln and not ln.lstrip().startswith("#")]
    assert lines == ["TEST_DB ?= korely_test"]


def _the_name_rule() -> re.Pattern:
    m = re.search(r"grep -Eq '(\^[^']+\$)\$'", _recipe("test-db"))
    assert m, "test-db checks the name before making the database"
    return re.compile(m.group(1))


@pytest.mark.parametrize("name", ["korely_test", "korely_test_r108", "korely_test_c17",
                                  "mytest", "test"])
def test_a_test_name_is_taken(name):
    assert _the_name_rule().fullmatch(name)


@pytest.mark.parametrize("name", ["korely", "korely_prod", "Korely_Test", "korely_test;drop",
                                  "korely test", "korely_test'", "korely-test", ""])
def test_any_other_name_is_refused(name):
    """The rule of scripts/check_migration.py --wipe (the name says `test`),
    and only the characters Postgres keeps as typed, which also keeps the name
    whole inside the recipe's shell quotes."""
    assert not _the_name_rule().fullmatch(name)


def _run_recipe(target: str, tmp_path, **env) -> tuple[int, str, list[str]]:
    """The lines of `target` itself, as make runs them: each in its own sh,
    `$$` as `$`, stopping at the first that fails; the test Postgres's
    compose (`$(TEST_COMPOSE)`) is a fake that writes its arguments down."""
    calls = tmp_path / "compose-calls"
    fake = tmp_path / "compose"
    fake.write_text('#!/bin/sh\necho "$*" >> "$CALLS"\n')
    fake.chmod(0o755)
    body = _recipe(target).split("\n", 1)[1].replace("\\\n", " ")
    said = ""
    for line in body.splitlines():
        line = line.strip().lstrip("@").replace("$$", "$").replace("$(TEST_COMPOSE)", '"$FAKE"')
        r = subprocess.run(["sh", "-c", line], cwd=ROOT, capture_output=True, text=True,
                           env={**os.environ, **env, "FAKE": str(fake), "CALLS": str(calls)})
        said += r.stderr
        if r.returncode:
            break
    made = calls.read_text().splitlines() if calls.exists() else []
    return r.returncode, said, made


@pytest.mark.skipif(shutil.which("sh") is None, reason="no sh here")
def test_the_drop_refuses_the_database_every_checkout_shares(tmp_path):
    """`make test-db-drop` with no TEST_DB took the default, the database the
    test Postgres makes for everyone, and dropped it (review of 15 Nov
    2026)."""
    code, said, made = _run_recipe("test-db-drop", tmp_path, TEST_DB="korely_test")
    assert code == 2 and "refused" in said, said
    assert not [c for c in made if "dropdb" in c], made


@pytest.mark.skipif(shutil.which("sh") is None, reason="no sh here")
def test_the_drop_drops_the_database_a_run_named(tmp_path):
    code, said, made = _run_recipe("test-db-drop", tmp_path, TEST_DB="korely_test_r108")
    assert code == 0, said
    assert made == ["exec -T db dropdb -U korely --if-exists korely_test_r108"], made


def test_a_second_run_on_the_database_of_a_run_is_refused(engine):
    """Two runs given one TEST_DB emptied each other's tables halfway through
    (a suite started twice on one name, 7 Nov 2026: tens of failures, then an
    hour hung). The run holds a lock on its database for its whole life, so
    a second one is refused before it empties anything, and the lock goes
    with the run's connection."""
    from sqlalchemy import create_engine, text

    from tests.conftest import hold_the_database

    other = create_engine(engine.url, future=True)
    try:
        with pytest.raises(pytest.UsageError, match="another run is using the test database"):
            hold_the_database(other)
        with other.connect() as c:
            assert c.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                                  " AND pid = pg_backend_pid()")).scalar() == 0
    finally:
        other.dispose()
