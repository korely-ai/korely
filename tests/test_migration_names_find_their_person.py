"""e7c4b2d9a6f1: the names 0.2.0 filed under the Default project go to their
person's project (audit of 27 Oct 2026).

Up to 0.1.40 POST /admin/aliases without a project wrote the name with none,
and a9d3f6c2e8b4 filed those under the Default project. An erasure in the
person's project then reached into the Default project for them, and there
deleted the names a namesake had declared. The upgrade places each
name where its person lives, so the erasure stays behind its project.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.conftest import TEST_DB, server_url
from tests.test_migrations import _alembic

_BEFORE, _THIS = "f6a3d9c1b8e2", "e7c4b2d9a6f1"


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


def _account(c):
    t = uuid.uuid4()
    c.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"), {"id": t})
    projects = {}
    for name in ("Default", "Beta", "Gamma"):
        projects[name] = uuid.uuid4()
        c.execute(text("INSERT INTO agent_projects (id, tenant_id, name, slug, is_default)"
                       " VALUES (:id, :t, :n, :s, :d)"),
                  {"id": projects[name], "t": t, "n": name, "s": name.lower(),
                   "d": name == "Default"})
    return t, projects


def _memory(c, t, project, user=None, agent=None):
    c.execute(text("INSERT INTO agent_memories (id, tenant_id, project_id, end_user_id, agent_id,"
                   " content, graph_sync_status) VALUES (:id, :t, :p, :u, :a, 'dummy', 'done')"),
              {"id": uuid.uuid4(), "t": t, "p": project, "u": user, "a": agent})


def _alias(c, t, project, label, *, user=None, agent=None, since=None, made=None):
    rid = uuid.uuid4()
    c.execute(text("INSERT INTO agent_entity_aliases (id, tenant_id, project_id, end_user_id,"
                   " agent_id, alias_lower, alias_label, canonical_label, since, created_at)"
                   " VALUES (:id, :t, :p, :u, :a, lower(:l), :l, 'Dummy Rossi',"
                   " COALESCE(CAST(:s AS timestamptz), CAST(:m AS timestamptz), now()),"
                   " COALESCE(CAST(:m AS timestamptz), now()))"),
              {"id": rid, "t": t, "p": project, "u": user, "a": agent, "l": label,
               "s": since, "m": made})
    return rid


_MADE = '{"setting": "alias", "op": "create"}'


def _trail(c, t, project, target, meta, key=None, actor="rest", at=None):
    """The request's own row, a second after `at` (now, by default)."""
    c.execute(text("INSERT INTO audit_events (id, ts, tenant_id, project_id, api_key_id, actor,"
                   " action, result, target_id, meta) VALUES (:id,"
                   " COALESCE(CAST(:at AS timestamptz) + interval '1 second', now()),"
                   " :t, :p, :k, :ac, :a, 'ok', :tg, CAST(:m AS json))"),
              {"id": uuid.uuid4(), "t": t, "p": project, "k": key, "tg": str(target),
               "ac": actor, "at": at,
               "a": "fact_write" if "endpoint" in meta else "setting_change", "m": meta})


def _key(c, t, project):
    kid = uuid.uuid4()
    c.execute(text("INSERT INTO agent_api_keys (id, tenant_id, project_id, name, key_hash,"
                   " prefix, scopes) VALUES (:id, :t, :p, :h, :h, 'kor_self_',"
                   " CAST(:sc AS json))"),
              {"id": kid, "t": t, "p": project, "h": uuid.uuid4().hex, "sc": '["admin"]'})
    return kid


def test_each_name_goes_to_its_persons_project(scratch):
    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    early = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with eng.begin() as c:
        t, p = _account(c)
        D, B, G = p["Default"], p["Beta"], p["Gamma"]
        _memory(c, t, B, user="u-beta")
        _memory(c, t, B, agent="bot-beta")
        _memory(c, t, D, user="u-home")
        _memory(c, t, B, user="u-home")
        for project in (B, G):
            _memory(c, t, project, user="u-two")
            _memory(c, t, project, user="u-two-key")
        # Made with no project up to 0.1.40, filed under Default by 0.2.0.
        legacy = _alias(c, t, D, "My Sister", user="u-beta")
        bot = _alias(c, t, D, "The Lab", agent="bot-beta")
        home = _alias(c, t, D, "My Wife", user="u-home")
        two = _alias(c, t, D, "My Wife", user="u-two")
        by_key = _alias(c, t, D, "My Wife", user="u-two-key")
        _trail(c, t, None, by_key, _MADE, key=_key(c, t, G))
        nowhere = _alias(c, t, D, "My Wife", user="u-nowhere")
        # The namesake in Default who only declared names, through /v1.
        declared = _alias(c, t, D, "My Boss", user="u-beta")
        _trail(c, t, D, declared, '{"endpoint": "names:declare"}')
        # The same name already in Beta, older: the newer decision stays.
        _alias(c, t, B, "The Lab", agent="bot-beta", since=early)
        newer = _alias(c, t, D, "The Shop", user="u-beta")
        older = _alias(c, t, B, "The Shop", user="u-beta", since=early - timedelta(days=1))
        # Filed in Default on purpose from 0.1.41 on (the door put a name
        # sent with no project in the key's project): by a key of Default,
        # the dashboard, a key of no project. They stay, for a person who
        # lives in Beta only.
        _memory(c, t, B, user="u-solo")
        d_key = _alias(c, t, D, "Dummy A", user="u-solo")
        _trail(c, t, None, d_key, _MADE, key=_key(c, t, D))
        dash = _alias(c, t, D, "Dummy B", user="u-solo")
        _trail(c, t, None, dash, '{"setting": "alias", "op": "create", "operator": "x"}',
               actor="dashboard")
        no_project = _alias(c, t, D, "Dummy C", user="u-solo")
        _trail(c, t, None, no_project, _MADE, key=_key(c, t, None))
        # The same key of Default before 0.1.41: the door wrote no project.
        before = datetime(2026, 10, 10, tzinfo=timezone.utc)
        d_key_old = _alias(c, t, D, "Dummy D", user="u-solo", made=before)
        _trail(c, t, None, d_key_old, _MADE, key=_key(c, t, D), at=before)
        # A request that re-canonicalized for two minutes before its commit:
        # its trail row is still its own.
        after = datetime(2026, 10, 20, tzinfo=timezone.utc)
        slow = _alias(c, t, D, "Dummy E", user="u-solo", made=after)
        _trail(c, t, None, slow, _MADE, key=_key(c, t, D), at=after + timedelta(seconds=119))
        # Filed in Default on purpose for a person who lives there: kept as is.
        home_key = _alias(c, t, D, "Dummy F", user="u-home")
        _trail(c, t, None, home_key, _MADE, key=_key(c, t, D))
    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        where = dict(c.execute(text("SELECT id, project_id FROM agent_entity_aliases")).all())
        marked = {r for (r,) in c.execute(text(
            "SELECT id FROM agent_entity_aliases WHERE project_not_recorded"))}
    assert where[legacy] == B, "a name of a person who lives in Beta only stayed in Default"
    assert where[bot] == B, "an agent namespace's name stayed in Default"
    assert where[declared] == D, "a name declared in Default left it"
    assert where[home] == D, "a person who lives in Default keeps the name there"
    assert where[two] == D, "a name nobody can attribute stays in Default"
    assert where[by_key] == G, "the declaring key's project decides between the person's two"
    assert where[nowhere] == D
    # Kept in Default by the time rule for a person who lives in Beta only:
    # marked, so the person's erasure there still takes them (an install
    # still on 0.1.40 after that day made them with no project).
    assert marked == {two, nowhere, d_key, dash, no_project, slow}, (
        "the names nobody can attribute are marked")
    assert where[home_key] == D
    assert where[newer] == B and older not in where
    assert (where[d_key], where[dash], where[no_project], where[slow]) == (D, D, D, D), (
        "a name filed in Default on purpose left it")
    assert where[d_key_old] == B, "a name made with no project before 0.1.41 stayed in Default"
    # Beta's older "The Lab" gave way to the newer decision from Default.
    assert {k for k, v in where.items() if v == B} == {legacy, bot, newer, d_key_old}
    _alembic(url, "downgrade", _BEFORE)
    with eng.connect() as c:
        assert c.execute(text("SELECT project_id FROM agent_entity_aliases WHERE id = :id"),
                         {"id": legacy}).scalar_one() == B
