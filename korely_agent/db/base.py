"""Declarative base and session factory.

The engine is built on first use, not at import time. Same reason the settings
object is lazy: importing a module must never require a reachable database, or
tooling that only wants to read the metadata (alembic autogenerate, a schema
dump, a test that touches no rows) fails for the wrong reason.
"""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache(maxsize=1)
def get_engine():
    """The process's one engine. Its pool's size and checkout wait, and the
    deadlines of a request's transaction (a lock wait, a statement), are
    core/threads.py's, where every resource the accounts share is bounded
    (audit of 20 Oct 2026: a request waited on
    another transaction's row lock with no end, holding a connection of this
    pool)."""
    from ..core import threads

    return _an_engine(threads.engine_pool())


def _an_engine(pool: dict):
    """An engine on the settings' database with `pool` (core/threads), and
    the request's deadlines: the one way both engines are made."""
    from ..core import threads

    s = get_settings()
    engine = create_engine(
        s.database_url,
        echo=s.sql_echo,
        pool_pre_ping=True,   # a recycled connection after an idle night is the
                              # single most common cause of a first-request 500
        future=True,
        # A failed statement used to carry its parameters into its message,
        # "[parameters: {...}]": a memory's text, an end user's id, a
        # vector, on any error, a deadlock or a dropped connection as much as
        # a bad value. That message reaches the logs, where an erasure never
        # does (2026-10-06, the rule of memory_core 3.0.0's `log_safe`).
        hide_parameters=True,
        **pool,
    )
    threads.with_deadlines(engine)
    return engine


@lru_cache(maxsize=1)
def get_sessionmaker():
    """Sessions of the main pool: the one a unit of work holds (a request's,
    a worker tick's, an admission job's)."""
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


@lru_cache(maxsize=1)
def get_own_engine():
    """The engine of the own sessions: the same database, a pool of its own
    (core/threads.own_engine_pool), the same deadlines. The audit of 27 Oct
    2026: a request that held a connection of the main pool and asked
    it for a second one, for its trail row, waited on the other holders,
    each waiting the same way."""
    from ..core import threads

    return _an_engine(threads.own_engine_pool())


@lru_cache(maxsize=1)
def get_own_sessionmaker():
    """A short session of its own, for work that may run while its caller
    holds a session of the main pool (the trail, the Cloud's counts, the
    model gate, the costs, the provider's settings). Its holder asks for no
    other connection while it holds it (tests/test_shared_resources_are_
    bounded_here.py holds that every main session is a unit of work's)."""
    return sessionmaker(bind=get_own_engine(), autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI dependency: one session per request, always closed."""
    db: Session = get_sessionmaker()()
    try:
        yield db
    finally:
        db.close()
