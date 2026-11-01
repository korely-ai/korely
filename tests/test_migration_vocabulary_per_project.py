"""The migration that splits the vocabulary by project (d6a2f8c4e1b7), on data.

A relation stays with the project that used it first and is copied into every
other project whose facts use it; each copy's example is a value of its own
project's facts, never another project's; a relation no fact uses goes to the
Default project. The downgrade puts one vocabulary per account back.

On a database of its own, made and dropped here, migrated with Alembic.
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


BEFORE, AFTER = "c3f5a7e9b1d4", "d6a2f8c4e1b7"
from korely_agent.core.llm import EMBEDDING_DIM  # noqa: E402
VECTOR = "[" + ",".join(["0.1"] * EMBEDDING_DIM) + "]"


@pytest.fixture
def scratch_db(monkeypatch):
    from tests.conftest import TEST_DB

    url = make_url(TEST_DB)
    name = f"korely_mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    target = url.set(database=name)
    with create_engine(target).begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    monkeypatch.setenv("DATABASE_URL", target.render_as_string(hide_password=False))
    from korely_agent.config import get_settings
    get_settings.cache_clear()
    yield target
    get_settings.cache_clear()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _alembic(direction, rev):
    from alembic import command
    from alembic.config import Config

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # No ini file: alembic/env.py runs logging's fileConfig on it, which
    # disables every logger already made, and every later test that reads the
    # log (caplog) found it empty (35 of them, the suite of 18 Oct 2026).
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(root, "alembic"))
    getattr(command, direction)(cfg, rev)


def test_the_vocabulary_is_split_by_use_and_each_example_is_its_projects(scratch_db):
    _alembic("upgrade", BEFORE)
    eng = create_engine(scratch_db)
    t, eu, glob, default = (uuid.uuid4() for _ in range(4))
    covered, unused = uuid.uuid4(), uuid.uuid4()
    with eng.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:t, 'Acme')"), {"t": t})
        for pid, name, is_default in ((default, "Default", True), (eu, "EU", False),
                                      (glob, "Global", False)):
            c.execute(text("INSERT INTO agent_projects (id, tenant_id, name, slug, is_default) "
                           "VALUES (:p, :t, :n, :n, :d)"), {"p": pid, "t": t, "n": name, "d": is_default})
        # The EU project used `covered_under` first; its value is the example.
        for pid, obj, when in ((eu, "PolicyAlpha", "2026-10-01"), (glob, "PolicyBeta", "2026-10-02"),
                               (glob, "PolicyGamma", "2026-10-03")):
            c.execute(text(
                "INSERT INTO agent_facts (id, tenant_id, project_id, subject_label, subject_canonical, "
                "predicate_raw, predicate_canonical, object_label, object_canonical, created_at) VALUES "
                "(:id, :t, :p, 'Customer 1', 'Customer 1', 'covered under', 'covered_under', :o, :o, :w)"),
                {"id": uuid.uuid4(), "t": t, "p": pid, "o": obj, "w": when})
        c.execute(text(
            "INSERT INTO predicate_clusters (id, tenant_id, canonical, is_functional, family, "
            "example_object, member_count, centroid) VALUES "
            "(:c, :t, 'covered_under', true, 'work', 'PolicyAlpha', 2, :v), "
            "(:u, :t, 'mentors', NULL, NULL, 'Elena', 1, :v)"),
            {"c": covered, "u": unused, "t": t, "v": VECTOR})
        c.execute(text(
            "INSERT INTO predicate_aliases (tenant_id, surface, cluster_id) VALUES "
            "(:t, 'covered_under', :c), (:t, 'insured_by', :c), (:t, 'mentors', :u)"),
            {"t": t, "c": covered, "u": unused})

    _alembic("upgrade", AFTER)
    with eng.connect() as c:
        rel = {(r.canonical, r.project_id): r for r in c.execute(text(
            "SELECT * FROM predicate_clusters WHERE tenant_id = :t"), {"t": t})}
        assert set(rel) == {("covered_under", eu), ("covered_under", glob), ("mentors", default)}
        assert rel[("covered_under", eu)].id == covered, "the first user keeps the row"
        assert rel[("covered_under", eu)].example_object == "PolicyAlpha"
        assert rel[("covered_under", glob)].example_object == "PolicyBeta", "its own value"
        assert rel[("covered_under", glob)].is_functional is True, "the vote travels"
        assert rel[("mentors", default)].example_object is None, "no fact of its project says it"
        # A centroid is the vector of the name with the example (code audit of
        # 18 Oct 2026): kept where the example is the one it was made with,
        # gone from a copy and from a row whose example changed.
        assert rel[("covered_under", eu)].centroid is not None
        assert rel[("covered_under", glob)].centroid is None, "made from another project's value"
        assert rel[("mentors", default)].centroid is None, "its example went"
        phrases = {(r.surface, r.project_id) for r in c.execute(text(
            "SELECT surface, project_id FROM predicate_aliases WHERE tenant_id = :t"), {"t": t})}
        assert phrases == {("covered_under", eu), ("insured_by", eu), ("covered_under", glob),
                           ("insured_by", glob), ("mentors", default)}

    _alembic("downgrade", BEFORE)
    with eng.connect() as c:
        rows = c.execute(text("SELECT canonical FROM predicate_clusters WHERE tenant_id = :t "
                              "ORDER BY canonical"), {"t": t}).scalars().all()
        assert rows == ["covered_under", "mentors"]
        assert c.execute(text("SELECT count(*) FROM predicate_aliases WHERE tenant_id = :t"),
                         {"t": t}).scalar() == 3
    eng.dispose()
