"""A failed statement does not print its parameters (2026-10-06).

SQLAlchemy formats the bound parameters into the message of a failed
statement, "[parameters: {...}]": a memory's text, an end user's id, a vector,
whatever the statement carried. The cause need not be the value: a deadlock, a
statement timeout or a dropped connection prints them all the same, and the
message goes to the logs (`docker compose logs`), where an erasure never
reaches. Both engines are built with `hide_parameters=True`.
"""
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

SAID = "Maria told the agent about her diagnosis"


def test_a_failed_statement_keeps_its_parameters_to_itself(engine):
    # `engine`: the test database, which this reaches through the settings.
    from korely_agent.db.base import get_engine

    with pytest.raises(DBAPIError) as e:
        with get_engine().connect() as c:
            # Fails for a reason that has nothing to do with the value.
            c.execute(text("SELECT CAST(:said AS text), 1 / 0"), {"said": SAID})
    assert "division by zero" in str(e.value)
    assert SAID not in str(e.value)


def test_the_migrations_engine_hides_them_too():
    env = (Path(__file__).resolve().parents[1] / "alembic" / "env.py").read_text()
    assert "hide_parameters=True" in env
