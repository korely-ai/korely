"""Alembic environment.

The database URL is read from the application settings rather than from
alembic.ini, so there is exactly one place that decides which database this
talks to and no checked-in file can point a migration somewhere unintended.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from korely_agent.config import get_settings
# From the package: it imports every model, which is what puts the tables on
# this metadata (autogenerate compares against it).
from korely_agent.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Through configparser, which reads `%` as the start of an interpolation: a
# password with `%`, or any character the URL had to quote, raised a ValueError
# whose message is the whole URL, password included (audit of 17 Oct 2026).
# Doubled, it comes back out of get_main_option as it went in.
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    """Every object is compared with the models, but the Cloud's tables on a
    tree without the Cloud's package (the public repository): their
    migrations are in the one chain, their models are not in the tree
    (core/extensions.cloud_tables_without_models). And never the tables of a
    part of the product that has a chain of its own, nor their indexes
    (core/extensions.Part.migrations, db/chains.py): that chain makes and
    compares them."""
    from korely_agent.core import extensions

    # A part's chain keeps its own version table too.
    if type_ == "table" and name.startswith("alembic_version"):
        return False
    own = extensions.part_tables()
    table = name if type_ == "table" else getattr(getattr(obj, "table", None), "name", None)
    if table in own:
        return False
    return not (type_ == "table" and reflected and compare_to is None
                and extensions.cloud_tables_without_models([name]))


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # As the application's engine (korely_agent/db/base.py): a failed
        # migration prints its statement, never the rows' values.
        hide_parameters=True,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            # Two processes starting on one database each run this, the
            # entrypoint's `alembic upgrade head`: without a lock both read the
            # same revision and both apply the next one, and the second dies on
            # the first one's table (two APIs on a new database, 30 Oct 2026).
            # Held to the commit; the next process then reads head and has
            # nothing to do. The key is the codebase's form (services/keys.py).
            connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('alembic-upgrade'))"))
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
