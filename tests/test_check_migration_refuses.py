"""scripts/check_migration.py drops the schema it is pointed at. It used to
drop whatever DATABASE_URL said, the shell's or a `.env` in the current
directory, with no question asked (audit of 17 Oct 2026). Now it empties only
a database named on its command line, whose name says it is a test one."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def check(monkeypatch):
    spec = importlib.util.spec_from_file_location("check_migration", ROOT / "scripts" / "check_migration.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    touched = []

    def create_engine(url, **kw):
        touched.append(url)
        raise RuntimeError("reached the database")

    monkeypatch.setattr(mod, "create_engine", create_engine)

    def run(url, *argv):
        monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(database_url=url))
        try:
            return mod.main(list(argv)), touched
        except RuntimeError:
            return "reached", touched
    return run


def test_without_the_flag_it_touches_nothing(check, capsys):
    code, touched = check("postgresql+psycopg2://korely:pw@db:5432/korely_test")
    assert code == 2 and touched == []
    assert "--wipe korely_test" in capsys.readouterr().err


def test_a_production_database_is_refused_even_when_named(check):
    code, touched = check("postgresql+psycopg2://korely:pw@db:5432/korely", "--wipe", "korely")
    assert code == 2 and touched == []


def test_the_name_must_be_the_one_the_url_points_at(check):
    code, touched = check("postgresql+psycopg2://korely:pw@db:5432/korely", "--wipe", "korely_test")
    assert code == 2 and touched == []


def test_a_test_database_named_on_the_command_line_is_emptied(check):
    code, touched = check("postgresql+psycopg2://korely:pw@localhost:55433/korely_test_c17",
                          "--wipe", "korely_test_c17")
    assert code == "reached" and len(touched) == 1


def test_every_caller_names_the_database():
    """`make migrate-check` names the database its URL points at: the one
    TEST_DB names, korely_test unless a run names its own."""
    makefile = (ROOT / "Makefile").read_text()
    assert "scripts/check_migration.py --wipe $(TEST_DB)" in makefile
    assert "-e DATABASE_URL=$(TEST_DB_URL)" in makefile
    assert "TEST_DB ?= korely_test\n" in makefile
