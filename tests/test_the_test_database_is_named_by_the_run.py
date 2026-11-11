"""The suite drops and creates every table of the database it is given, and
every checkout on a machine (worktrees, other sessions) shares the one test
Postgres on 55433. With the name written into the Makefile, a release run and
another session's run on the same machine would empty each other's tables
halfway through (seen on 4 Nov 2026, before the release of 1.0.8, with seven
sessions' worktrees on one Postgres). So the Makefile reads the name from
TEST_DB, which a run sets on make's command line or in the environment, and
creates that database when it is missing."""
import re
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
