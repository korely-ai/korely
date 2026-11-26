"""A write that read its row again after its commit (`commit`, then
`refresh`) answered 500 when a deletion of that row committed in between:
the write had happened, the answer said it had not, and a client retries a
500 (the finding of 14 Nov 2026, the siblings of a memory's write and a
part's PATCH). Every write now reads what the database made of its row
before its commit (db/base.commit_written). Each test forces that
interleaving with no sleep: the row is deleted in the write's own
`after_commit`, in another session, before anything else of the write
(tests/test_a_write_answers_what_it_wrote.py's way); and a scan holds the
product to it."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from korely_agent.db.models import (AgentApiKey, AgentBatchJob, AgentEntityAlias, AgentProject,
                                    AgentWebhookEndpoint, Operator, OperatorSession)
from korely_agent.services import batch, keys, operators, projects, webhooks
from tests import cloud

PACKAGE = Path(__file__).resolve().parents[1] / "korely_agent"


@pytest.fixture
def deleted_right_after(db):
    """`arm(model, find)`: right after the first commit of `db` once the row
    `find(session)` returns exists, that row of `model` is deleted in a
    session of its own (a write that commits other things first, as a route
    writes its trail, is waited for). `done` names each table deleted from."""
    other = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    armed, done = [], []

    def delete_now(_session):
        if not armed:
            return
        model, find = armed[0]
        s = other()
        try:
            row = find(s)
            if row is not None:
                armed.pop()
                s.delete(row)
                s.commit()
                done.append(model.__tablename__)
        finally:
            s.close()

    event.listen(db, "after_commit", delete_now)
    try:
        yield (lambda model, find: armed.append((model, find))), done
    finally:
        event.remove(db, "after_commit", delete_now)


@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient

    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _project(db, tenant, name="side"):
    return projects.create_project(db, tenant_id=tenant.id, name=name)


def test_the_default_project_made_and_deleted_at_once_answers_what_it_made(
        db, tenant, deleted_right_after):
    arm, done = deleted_right_after
    arm(AgentProject, lambda s: s.query(AgentProject).filter_by(
        tenant_id=tenant.id, is_default=True).one_or_none())
    row = projects.get_or_create_default_project(db, tenant.id)
    assert done == ["agent_projects"] and row.is_default and row.created_at is not None


def test_a_project_made_and_deleted_at_once_answers_what_it_made(db, tenant, deleted_right_after):
    arm, done = deleted_right_after
    arm(AgentProject, lambda s: s.query(AgentProject).filter_by(
        tenant_id=tenant.id, name="side").one_or_none())
    row = _project(db, tenant)
    assert done == ["agent_projects"], "the deletion did not run right after the commit"
    assert row.name == "side" and row.slug and row.created_at is not None


@pytest.mark.parametrize("act", ["rename", "archive", "unarchive"])
def test_a_project_changed_and_deleted_at_once_answers_its_change(db, tenant,
                                                                  deleted_right_after, act):
    arm, done = deleted_right_after
    row = _project(db, tenant)
    if act == "unarchive":
        row = projects.archive_project(db, row)
    pid = row.id
    arm(AgentProject, lambda s: s.get(AgentProject, pid))
    if act == "rename":
        row = projects.rename_project(db, row, name="other")
        assert row.name == "other"
    elif act == "archive":
        assert projects.archive_project(db, row).archived_at is not None
    else:
        assert projects.unarchive_project(db, row).archived_at is None
    assert done == ["agent_projects"]


def test_an_operator_made_and_deleted_at_once_answers_what_it_made(db, tenant,
                                                                    deleted_right_after):
    arm, done = deleted_right_after
    arm(Operator, lambda s: s.query(Operator).filter_by(
        tenant_id=tenant.id, username="anna").one_or_none())
    row, raw = operators.create(db, tenant_id=tenant.id, username="anna")
    assert done == ["operators"] and row.username == "anna" and raw


def test_a_session_opened_and_deleted_at_once_answers_what_it_opened(db, tenant,
                                                                      deleted_right_after):
    arm, done = deleted_right_after
    op, _pw = operators.create(db, tenant_id=tenant.id, username="anna")
    arm(OperatorSession, lambda s: s.query(OperatorSession).filter_by(
        operator_id=op.id).one_or_none())
    row, raw = operators.open_session(db, op)
    assert done == ["operator_sessions"] and row.expires_at is not None and raw


def test_a_key_made_and_deleted_at_once_answers_what_it_made(db, tenant, deleted_right_after):
    arm, done = deleted_right_after
    arm(AgentApiKey, lambda s: s.query(AgentApiKey).filter_by(
        tenant_id=tenant.id, name="made").one_or_none())
    row, raw = keys.create_api_key(db, tenant_id=tenant.id, name="made")
    assert done == ["agent_api_keys"] and row.created_at is not None and raw


def test_a_key_rotated_and_deleted_at_once_answers_its_new_secret(db, tenant,
                                                                  deleted_right_after):
    arm, done = deleted_right_after
    made, _raw = keys.create_api_key(db, tenant_id=tenant.id, name="turns")
    kid = made.id
    arm(AgentApiKey, lambda s: s.get(AgentApiKey, kid))
    row, raw = keys.rotate_api_key(db, key_id=kid, tenant_id=tenant.id)
    assert done == ["agent_api_keys"] and raw and row.rotated_at is not None


def test_a_batch_queued_and_deleted_at_once_answers_its_job(db, tenant, deleted_right_after):
    arm, done = deleted_right_after
    arm(AgentBatchJob, lambda s: s.query(AgentBatchJob).filter_by(
        tenant_id=tenant.id).one_or_none())
    job = batch.enqueue_batch(db, tenant.id, [{"content": "Anna likes tea."}])
    assert done == ["agent_batch_jobs"] and job.received == 1 and job.created_at is not None


def _endpoint(db, tenant, monkeypatch, **kw):
    monkeypatch.setattr(webhooks, "check_url", lambda u: u.strip())
    return webhooks.create_endpoint(db, tenant.id, url="https://hooks.example/x",
                                    events=["*"], **kw)


def test_a_webhook_made_and_deleted_at_once_answers_what_it_made(db, tenant, monkeypatch,
                                                                 deleted_right_after):
    arm, done = deleted_right_after
    arm(AgentWebhookEndpoint, lambda s: s.query(AgentWebhookEndpoint).filter_by(
        tenant_id=tenant.id).one_or_none())
    ep = _endpoint(db, tenant, monkeypatch)
    assert done == ["agent_webhook_endpoints"] and ep.secret and ep.created_at is not None


@pytest.mark.parametrize("act", ["rotate", "enable"])
def test_a_webhook_changed_and_deleted_at_once_answers_its_change(db, tenant, monkeypatch,
                                                                  deleted_right_after, act):
    arm, done = deleted_right_after
    ep = _endpoint(db, tenant, monkeypatch)
    eid, secret = ep.id, ep.secret
    if act == "enable":
        ep.active = False
        db.commit()
    arm(AgentWebhookEndpoint, lambda s: s.get(AgentWebhookEndpoint, eid))
    if act == "rotate":
        out = webhooks.rotate_secret(db, tenant.id, eid)
        assert out is not None and out.secret != secret
    else:
        out = webhooks.enable_endpoint(db, tenant.id, eid)
        assert out is not None and out.active
    assert done == ["agent_webhook_endpoints"]


def test_a_name_declared_and_deleted_at_once_answers_it(db, tenant, client,
                                                        deleted_right_after):
    arm, done = deleted_right_after
    _row, full = keys.create_api_key(db, tenant_id=tenant.id, name="names")
    arm(AgentEntityAlias, lambda s: s.query(AgentEntityAlias).filter_by(
        tenant_id=tenant.id, end_user_id="42-anna").first())
    r = client.post("/v1/users/42-anna/names", headers={"Authorization": f"Bearer {full}"},
                    json={"name": "Anna Rossi"})
    assert r.status_code in (200, 201), r.text
    assert done == ["agent_entity_aliases"]


def test_an_alias_made_and_deleted_at_once_answers_it(db, tenant, client, deleted_right_after):
    from korely_agent.services.facts import service

    arm, done = deleted_right_after
    _row, full = keys.create_api_key(db, tenant_id=tenant.id, name="admin",
                                     scopes=list(keys.DEFAULT_SCOPES) + [keys.ADMIN_SCOPE])
    default = projects.get_or_create_default_project(db, tenant.id).id
    service.write_fact_direct(db, tenant.id, subject="Acme", predicate="based_in", obj="Milan",
                              end_user_id="maria", subject_type="organization",
                              project_id=default)
    arm(AgentEntityAlias, lambda s: s.query(AgentEntityAlias).filter_by(
        tenant_id=tenant.id, alias_label="Acme").first())
    r = client.post("/admin/aliases", headers={"Authorization": f"Bearer {full}"},
                    json={"alias": "Acme", "canonical": "Globex", "user_id": "maria"})
    assert r.status_code == 201, r.text
    assert done == ["agent_entity_aliases"]


# The writes that answer the row they wrote (each tested above): each
# commits through db.commit_written, and with no commit of its own.
WRITES = {
    "api/facts.py": {"declare_name"}, "api/admin.py": {"create_alias"},
    "services/batch.py": {"enqueue_batch"},
    "services/projects.py": {"get_or_create_default_project", "create_project",
                             "rename_project", "archive_project", "unarchive_project"},
    "services/keys.py": {"create_api_key", "reissue_secret"},
    "services/webhooks.py": {"create_endpoint", "rotate_secret", "enable_endpoint"},
    "services/operators.py": {"create", "open_session"},
    "services/memory_engine.py": {"add", "update"},
}


def _calls(fn, attr):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)
            and (isinstance(n.func, ast.Attribute) and n.func.attr == attr
                 or isinstance(n.func, ast.Name) and n.func.id == attr)]


def test_every_write_of_the_list_commits_through_commit_written():
    wrong = []
    for rel, names in sorted(WRITES.items()):
        tree = ast.parse((PACKAGE / rel).read_text(encoding="utf-8"))
        found = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in names:
                found[fn.name] = fn
        for name in sorted(names):
            fn = found.get(name)
            if fn is None:
                wrong.append(f"{rel}:{name} is not there")
            elif not _calls(fn, "commit_written") or [c for c in _calls(fn, "commit")
                                                      if not c.args]:
                wrong.append(f"{rel}:{fn.lineno} {name}")
    assert not wrong, "a write that answers its row commits by db.commit_written alone: " + \
        "; ".join(wrong)


def _refreshed_after_its_commit(source: str):
    """(function, line) of each `x.refresh(...)` right after `x.commit()`,
    or after a `try` whose body ends with it."""
    def commits(stmt, who):
        return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Attribute)
                and stmt.value.func.attr == "commit" and ast.unparse(stmt.value.func.value) == who)

    found = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for body in [n.body for n in ast.walk(fn) if hasattr(n, "body") and isinstance(n.body, list)] + \
                [n.orelse for n in ast.walk(fn) if isinstance(getattr(n, "orelse", None), list)]:
            for before, stmt in zip(body, body[1:]):
                if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                        and isinstance(stmt.value.func, ast.Attribute)
                        and stmt.value.func.attr == "refresh"):
                    continue
                who = ast.unparse(stmt.value.func.value)
                last = before.body[-1] if isinstance(before, ast.Try) and before.body else before
                if commits(last, who):
                    found.append((fn.name, stmt.lineno))
    return found


def test_no_write_reads_its_row_again_after_its_commit():
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        found += [f"{path.relative_to(PACKAGE).as_posix()}:{line} {fn}"
                  for fn, line in _refreshed_after_its_commit(path.read_text(encoding="utf-8"))]
    assert not found, ("a row read again after its commit: a deletion in between answers "
                       "500 for a write that happened; use db.commit_written: " + "; ".join(found))


def test_the_scan_sees_both_shapes():
    assert _refreshed_after_its_commit(
        "def f(db, r):\n    db.commit()\n    db.refresh(r)\n") == [("f", 3)]
    assert _refreshed_after_its_commit(
        "def f(db, r):\n    try:\n        db.commit()\n    except E:\n        raise\n"
        "    db.refresh(r)\n") == [("f", 6)]
    assert _refreshed_after_its_commit(
        "def f(db, r):\n    db.flush()\n    db.refresh(r)\n    db.commit()\n") == []


def _returned_after_a_bare_commit(source: str):
    """(function, line) of each `return x` (or a tuple holding x) after a
    plain `s.commit()` in a function that wrote x with `s.add(x)`: the row
    answered is read from the database again for any column the database
    made, after the commit (the shape the review of 26 Nov 2026 found
    passing both the tests and the scan above)."""
    found = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        added = {(ast.unparse(c.func.value), c.args[0].id) for c in _calls(fn, "add")
                 if isinstance(c.func, ast.Attribute) and c.args
                 and isinstance(c.args[0], ast.Name)}
        commits = [(ast.unparse(c.func.value), c.lineno) for c in _calls(fn, "commit")
                   if isinstance(c.func, ast.Attribute) and not c.args]
        for ret in (n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value is not None):
            values = ret.value.elts if isinstance(ret.value, ast.Tuple) else [ret.value]
            for v in values:
                if isinstance(v, ast.Name) and any(
                        (who, v.id) in added and line < ret.lineno for who, line in commits):
                    found.append((fn.name, ret.lineno))
    return sorted(set(found))


# Rows answered after a plain commit, found by the scan on 26 Nov 2026 and
# not yet looked at (whether a caller reads a column the database made):
# (file, function), taken off when it commits by db.commit_written, so the
# scan fails on a new one. Those of the Cloud's package are listed with it
# (tests/hosted_parts.py), as every guard of the core lists them.

REPORTED_NOT_YET_CHANGED = set(cloud.of_the_parts("BARE_COMMIT_REPORTED", set))


def test_no_write_answers_a_row_it_added_after_a_plain_commit():
    found = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if "memory_core" in path.parts:
            continue
        found |= {(rel, fn) for fn, _line in
                  _returned_after_a_bare_commit(path.read_text(encoding="utf-8"))}
    new = sorted(found - REPORTED_NOT_YET_CHANGED)
    gone = sorted(set(cloud.in_this_tree(REPORTED_NOT_YET_CHANGED)) - found)
    assert not new, "a row answered after a plain commit: use db.commit_written: " + str(new)
    assert not gone, "no longer found, take it off REPORTED_NOT_YET_CHANGED: " + str(gone)


def test_the_bare_commit_scan_sees_the_return_of_the_row():
    assert _returned_after_a_bare_commit(
        "def f(db):\n    ep = E()\n    db.add(ep)\n    db.commit()\n    return ep\n") == [("f", 5)]
    assert _returned_after_a_bare_commit(
        "def f(db):\n    ep = E()\n    db.add(ep)\n    commit_written(db, ep)\n    return ep\n") == []
