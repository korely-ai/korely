"""The chains of migrations a tree carries: the core's (alembic/), which has
one head and is the same on every tree, and one of each part of the product
the Cloud's package carries (core/extensions.Part.migrations), with its own
version table, applied after the core's because its tables point at the
core's. A tree without the package (the public repository) has the core's
alone, and this does nothing.

    alembic upgrade head                 # the core's (deploy/entrypoint.sh)
    python -m korely_agent.db.chains     # then every part's, to its head
    python -m korely_agent.db.chains --list   # the alembic.ini of each, one a line

The entrypoint applies each listed chain with the `alembic` command, as it
applies the core's: the same program, the same lock (alembic/env.py).
"""
from __future__ import annotations

import sys


def part_chains() -> list:
    """The alembic.ini of each part's own chain, in the parts' order."""
    from ..core import extensions

    return [ini for part in extensions.parts() if (ini := part.migrations())]


def absent_tables(db) -> set:
    """The tables of the parts' chains (core/extensions.part_tables) that the
    database `db` talks to does not have: their chain never ran there (an
    entrypoint that skipped it, a restore of before). Such a table holds no
    row, so an erasure, a count or a move has nothing of it to take, and
    asking it would fail the whole operation (the tester's finding of 5 Nov
    2026: an account could not close). One catalog lookup per table; none on
    a tree without the Cloud's package."""
    from sqlalchemy import text

    from ..core import extensions

    names = sorted(extensions.part_tables())
    if not names:
        return set()
    rows = db.execute(text("SELECT t FROM unnest(CAST(:names AS text[])) AS t "
                           "WHERE to_regclass(quote_ident(t)) IS NULL"), {"names": names})
    return {t for (t,) in rows}


def where(db, ini: str) -> tuple:
    """(the revisions the database `db` is at in the chain of `ini`, its
    head): () for the first when the chain never ran there."""
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    cfg = Config(ini)
    heads = tuple(ScriptDirectory.from_config(cfg).get_heads())
    context = MigrationContext.configure(
        db.connection(), opts={"version_table": cfg.get_main_option("version_table")})
    return tuple(context.get_current_heads()), heads


def upgrade_parts() -> int:
    """Every part's chain to its head, on the database DATABASE_URL names.
    How many chains were applied."""
    from alembic import command
    from alembic.config import Config

    chains = part_chains()
    for ini in chains:
        command.upgrade(Config(ini), "head")
    return len(chains)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--list"]:
        for ini in part_chains():
            print(ini)
        return 0
    if argv:
        print("usage: python -m korely_agent.db.chains [--list]", file=sys.stderr)
        return 64
    n = upgrade_parts()
    if n:
        print(f"{n} more chain{'s' if n != 1 else ''} of migrations applied", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
