"""The migration of memory_core 6.20.0 (a9d4c7e2f1b6), on data: every link written
before is a memory's ("memory"), a wall's link and the no-agent memory's link
of one name are two rows, the constraint refuses a link deleted with a
closing recorded on it, and the downgrade refuses while a closing is
recorded, then takes the wall's links away with the table and the column."""
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, ProgrammingError

from tests.conftest import TEST_DB, server_url
from tests.test_migrations import _alembic

_BEFORE, _THIS = "e5c9a2f7d3b1", "a9d4c7e2f1b6"


@pytest.fixture
def scratch():
    """(url, engine) of an empty database with pgvector, dropped afterwards
    (as tests/test_migrations.scratch)."""
    name = f"korely_migrations_{uuid.uuid4().hex[:8]}"
    admin = create_engine(server_url(), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(TEST_DB).set(database=name).render_as_string(hide_password=False)
    eng = create_engine(url)
    try:
        with eng.begin() as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        yield url, eng
    finally:
        eng.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _link(c, t, name, *, scope=None):
    rid = uuid.uuid4()
    cols, vals = "", ""
    if scope is not None:
        cols, vals = ", scope", ", :scope"
    c.execute(text("INSERT INTO agent_object_links (id, tenant_id, name_label, canonical_label, family, decided_by"
                   f"{cols}) VALUES (:id, :t, :n, 'Cloud', 'other', 'review'{vals})"),
              {"id": rid, "t": t, "n": name, "scope": scope})
    return rid


def _fact(c, t):
    fid = uuid.uuid4()
    c.execute(text("INSERT INTO agent_facts (id, tenant_id, subject_label, predicate_raw, predicate_canonical,"
                   " object_label, confidence) VALUES (:id, :t, 'Korely Cloud', 'has_version', 'has_version',"
                   " '1.0.30', 0.9)"), {"id": fid, "t": t})
    return fid


def test_up_and_down_on_data(scratch):
    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    t = uuid.uuid4()
    with eng.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"), {"id": t})
        old = _link(c, t, "Korely Cloud")
    _alembic(url, "upgrade", _THIS)
    with eng.begin() as c:
        assert c.execute(text("SELECT scope FROM agent_object_links WHERE id = :i"), {"i": old}).scalar() == \
            "memory", "every link written before is a memory's"
        wall = _link(c, t, "Korely Cloud", scope="wall")
        f = _fact(c, t)
        c.execute(text("INSERT INTO agent_fact_closure_links (tenant_id, fact_id, link_id) VALUES (:t, :f, :l)"),
                  {"t": t, "f": f, "l": wall})
    with pytest.raises(IntegrityError):
        with eng.begin() as c:
            _link(c, t, "Korely Cloud", scope="wall")    # one name per wall
    with pytest.raises(IntegrityError):
        with eng.begin() as c:
            _link(c, t, "Cloud di Korely", scope="team")
    with pytest.raises(IntegrityError):
        with eng.begin() as c:
            c.execute(text("DELETE FROM agent_object_links WHERE id = :l"), {"l": wall})
    with eng.begin() as c:
        # The fact and its link together, in one statement: the constraint is
        # checked at its end, and passes.
        c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": t})
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"), {"id": t})
        old = _link(c, t, "Korely Cloud")
        wall = _link(c, t, "Korely Cloud", scope="wall")
        c.execute(text("INSERT INTO agent_fact_closure_links (tenant_id, fact_id, link_id) VALUES (:t, :f, :l)"),
                  {"t": t, "f": _fact(c, t), "l": wall})
    with pytest.raises(Exception, match="closings are recorded"):
        _alembic(url, "downgrade", _BEFORE)
    with eng.begin() as c:
        c.execute(text("DELETE FROM agent_fact_closure_links"))
    _alembic(url, "downgrade", _BEFORE)
    with eng.begin() as c:
        assert [r[0] for r in c.execute(text("SELECT id FROM agent_object_links"))] == [old]
        with pytest.raises(ProgrammingError):
            c.execute(text("SELECT scope FROM agent_object_links"))
