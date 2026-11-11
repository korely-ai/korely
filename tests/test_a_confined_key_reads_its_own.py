"""A key confined to some end users (services/user_scope.py) reads its own
end users only, on every /v1 route and every MCP tool (audit of 20 Oct
2026: a batch job's status was not bound to the confined key).

GET /v1/batch/{job_id} was the one aggregate a confined key read whole: the
counts and the per-item errors of a job another key queued with other end
users' items, the job's id handed to it by its own GET /v1/audit. The guard
below walks every /v1 route the app serves and every tool of the MCP: each
one asks the key's end users (directly or through a helper of its module),
or is named here with why it needs not. The routes are the app's own
(main.create_app, and the Cloud's routers where this tree has them), each
read in the module that defines it, so a module with /v1 routes is walked
without anybody listing it (6 Nov 2026, the shared level's specification:
the documents' routes, on a branch of their own, were walked by nobody).
"""
import ast
import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests import cloud

ROOT = Path(__file__).resolve().parent.parent

# Asking any of these is asking the key's end users (core/auth.py).
_ASKS = {"key_end_users", "reading", "writing", "sees_row", "_within", "_end_users"}
# The routes and tools that read nothing of an end user, each with why.
_NEED_NOT = cloud.in_this_tree({
    # The key reads its own name, scopes and project: nobody's data.
    ("korely_agent/api/ping.py", "ping"),
    # The Cloud's sign-up: no key yet, it makes the account and its first.
    ("korely_agent/hosted/api_identity.py", "init_agent"),
    # The whole account, with the `admin` scope, which a confined key never
    # holds (services/keys.py, BadEndUsers).
    ("korely_agent/hosted/api_identity.py", "delete_account"),
})


def _endpoints():
    """(file, function, route) of every /v1 route the app serves, the
    Cloud's included where this tree has them, and of every MCP tool."""
    from korely_agent.api.errors import api_routes, every_route
    from korely_agent.main import create_app
    from korely_agent.mcp import server as mcp_server

    routes = list(every_route(create_app().routes))
    if cloud.HERE:
        from korely_agent import hosted

        routes += [r for router in hosted.routers() for r in every_route(router.routes)]
    v1 = [r for r in routes if getattr(r, "path", "").startswith("/v1")]
    readable = list(api_routes(v1))
    assert len(readable) == len(v1), "a /v1 route the guard cannot read"
    out = [(r.endpoint, f"{sorted(r.methods)} {r.path}") for r in readable]
    for name, tool in mcp_server._get_agent_mcp()._tool_manager._tools.items():
        out.append((tool.fn, f"MCP {name}"))
    for fn, what in out:
        fn = inspect.unwrap(fn)
        rel = Path(inspect.getsourcefile(fn)).resolve().relative_to(ROOT).as_posix()
        yield rel, fn.__name__, fn.__code__.co_firstlineno, what


def _doors():
    trees = {}
    for rel, name, line, what in _endpoints():
        if rel not in trees:
            trees[rel] = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        tree = trees[rel]
        defs = [f for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
        helpers = {f.name: f for f in defs}
        # The function itself: by its name, at the line its code starts
        # (its first decorator's, or its `def`).
        fn = next(f for f in defs if f.name == name and line in
                  {f.lineno} | {d.lineno for d in f.decorator_list})

        def names(fn, seen):
            out = ({n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
                   | {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)})
            for n in list(out):
                if n in helpers and n not in seen:
                    seen.add(n)
                    out |= names(helpers[n], seen)
            return out

        yield rel, name, names(fn, {name}), what


def test_every_v1_route_and_tool_is_walked():
    """The control: the routes walked are the app's, every module that has
    one among them, the Cloud's where this tree carries it."""
    doors = list(_doors())
    modules = {rel for rel, _n, _u, _w in doors}
    assert {"korely_agent/api/memories.py", "korely_agent/api/facts.py",
            "korely_agent/mcp/server.py"} <= modules, modules
    assert ("korely_agent/hosted/api_identity.py" in modules) == cloud.HERE
    assert sum(w.startswith("MCP ") for _r, _n, _u, w in doors) >= 6


def test_every_door_asks_the_keys_end_users():
    doors = list(_doors())
    assert len(doors) > 20, doors
    blind = {(rel, fn) for rel, fn, used, _w in doors if not used & _ASKS} - set(_NEED_NOT)
    assert blind == set(), blind


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                        lambda *a, **k: [])
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_a_confined_key_reads_only_the_jobs_it_queued(db, tenant, client):
    from korely_agent.services import batch
    from korely_agent.services import keys as key_service

    scopes = ["memories:read", "memories:write"]
    _w, whole = key_service.create_api_key(db, tenant_id=tenant.id, name="whole", scopes=scopes)
    _b, bob = key_service.create_api_key(db, tenant_id=tenant.id, name="bob", scopes=scopes,
                                         end_users=["bob"])
    whole_h, bob_h = ({"Authorization": f"Bearer {k}"} for k in (whole, bob))

    r = client.post("/v1/batch", headers=whole_h, json={"memories": [
        {"content": "bob row", "user_id": "bob"}, {"content": "anna row", "user_id": "anna"}]})
    assert r.status_code == 202, r.text
    theirs = r.json()["id"]
    r = client.post("/v1/batch", headers=bob_h, json={"memories": [
        {"content": "bob's own", "user_id": "bob"}]})
    assert r.status_code == 202, r.text
    own = r.json()["id"]
    batch.process_batch_jobs(db, limit=5)

    # The confined key finds the other job's id in its own trail...
    trail = client.get("/v1/audit", headers=bob_h, params={"limit": 500}).json()
    assert theirs in {(e.get("meta") or {}).get("batch") for e in trail["events"]}
    # ...and is told nothing of it, as of another project's job.
    r = client.get(f"/v1/batch/{theirs}", headers=bob_h)
    assert (r.status_code, r.json()["code"]) == (404, "not_found"), r.text
    assert client.get(f"/v1/batch/{own}", headers=bob_h).status_code == 200
    assert client.get(f"/v1/batch/{theirs}", headers=whole_h).json()["received"] == 2
