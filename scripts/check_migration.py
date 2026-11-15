"""Apply the migrations to an empty database and compare against the models.

The reason this exists: the codebase this was extracted from ran its tests on
SQLite, which does not apply migrations at all. A migration referencing a column
that did not exist passed every test, and then took production down in a boot
loop, because the container runs `alembic upgrade` before the web server. Nobody
saw it until the health check stopped answering.

It starts by dropping the schema of the database it is pointed at, so it
refuses any database it is not told, by name, to empty, and any whose name
does not say it is a test one (audit of 17 Oct 2026: it dropped whatever
DATABASE_URL said, from the shell or from a `.env` in the current directory,
and its first line here told everybody to run it before every release):

    python scripts/check_migration.py --wipe korely_test

`make migrate-check` runs it that way, and so does the tests workflow on every
release tag. Exits non-zero on drift, 2 on a database it will not touch.
"""
import argparse
import importlib.util
import subprocess
import sys

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from korely_agent.config import get_settings
# From the package: it imports every model, which is what puts the tables on
# this metadata, the side of the comparison the migrations are held to.
from korely_agent.db import Base


# The tables of the hosted service (korely_agent/hosted/models.py), which the
# one chain of migrations creates on every install and an install of one's own
# leaves empty (revision 6e186e7f9cad). The public repository has no hosted/
# package, so there the models do not declare them while the migrations still
# create them: that is the layout, not drift.
HOSTED_PREFIX = "hosted_"


def left_to_the_hosted_package(tables: set[str], hosted_present: bool) -> set[str]:
    """`tables` without those that belong to the hosted package when this tree
    does not carry it. With the package, nothing is set aside: the models
    declare them and the comparison covers them like any other."""
    if hosted_present:
        return set(tables)
    return {t for t in tables if not t.startswith(HOSTED_PREFIX)}


def refusal(url: str, wipe: str | None) -> str | None:
    """Why this database must not be emptied, or None when it may be. Two
    conditions, both needed: the name typed on the command line, so a copy
    whose .env points elsewhere stops instead of guessing, and a name that
    says `test`, so a slip of the finger on a real one stops too."""
    name = make_url(url).database or ""
    if not wipe:
        return (f"this drops every table of the database DATABASE_URL names "
                f"({name!r}). Say it is the one to empty: --wipe {name}")
    if wipe != name:
        return f"--wipe names {wipe!r}, and DATABASE_URL points at {name!r}: nothing done"
    if "test" not in name.lower():
        return (f"{name!r} is not a test database (its name does not say `test`): "
                f"this empties only a scratch database made for it")
    return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--wipe", metavar="DATABASE",
                   help="the name of the database DATABASE_URL points at, which "
                        "this empties; it must contain 'test'")
    a = p.parse_args(argv)

    url = get_settings().database_url
    why = refusal(url, a.wipe)
    if why:
        print(f"refused: {why}", file=sys.stderr)
        return 2
    eng = create_engine(url, hide_parameters=True)

    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    r = subprocess.run(["alembic", "-c", "alembic/alembic.ini", "upgrade", "head"], capture_output=True, text=True)
    if r.returncode != 0:
        print("migrations failed to apply:\n" + r.stderr, file=sys.stderr)
        return 1
    # Then the chains of the parts of the product the Cloud's package
    # carries, as the entrypoint applies them (korely_agent/db/chains.py):
    # none on a tree without the package.
    r = subprocess.run([sys.executable, "-m", "korely_agent.db.chains"], capture_output=True,
                       text=True)
    if r.returncode != 0:
        print("migrations failed to apply:\n" + r.stderr, file=sys.stderr)
        return 1

    hosted_present = importlib.util.find_spec("korely_agent.hosted") is not None
    have = left_to_the_hosted_package(
        {t for t in inspect(eng).get_table_names() if not t.startswith("alembic_version")},
        hosted_present)
    want = set(Base.metadata.tables)

    problems = []
    if have - want:
        problems.append(f"tables the migrations create but the models do not declare: {sorted(have - want)}")
    if want - have:
        problems.append(f"tables the models declare but the migrations do not create: {sorted(want - have)}")

    for t in sorted(have & want):
        cols_db = {c["name"] for c in inspect(eng).get_columns(t)}
        cols_model = set(Base.metadata.tables[t].c.keys())
        if cols_db != cols_model:
            problems.append(
                f"{t}: only in DB {sorted(cols_db - cols_model)}, "
                f"only in models {sorted(cols_model - cols_db)}"
            )

    if problems:
        print("SCHEMA DRIFT", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        return 1

    print(f"migrations apply cleanly: {len(have)} tables, matching the models")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
