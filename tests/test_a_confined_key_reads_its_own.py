"""A key confined to some end users (services/user_scope.py) reads its own
end users only, on every /v1 route and every MCP tool (audit of 20 Oct
2026: a batch job's status was not bound to the confined key).

GET /v1/batch/{job_id} was the one aggregate a confined key read whole: the
counts and the per-item errors of a job another key queued with other end
users' items, the job's id handed to it by its own GET /v1/audit. The guard
below walks every route of api/ and every tool of the MCP: each one asks the
key's end users (directly or through a helper of its module), or is named
here with why it needs not.
"""
import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent

# Asking any of these is asking the key's end users (core/auth.py).
_ASKS = {"key_end_users", "reading", "writing", "sees_row", "_within", "_end_users"}
# The routes and tools that read nothing of an end user, each with why.
_NEED_NOT = {
    # The key reads its own name, scopes and project: nobody's data.
    ("korely_agent/api/ping.py", "ping"),
}


def _doors():
    for rel in ("api/memories.py", "api/facts.py", "api/context.py", "api/audit.py",
                "api/batch.py", "api/ping.py", "mcp/server.py"):
        path = ROOT / "korely_agent" / rel
        tree = ast.parse(path.read_text())
        helpers = {f.name: f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)}

        def names(fn, seen):
            out = ({n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
                   | {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)})
            for n in list(out):
                if n in helpers and n not in seen:
                    seen.add(n)
                    out |= names(helpers[n], seen)
            return out

        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            door = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                       and d.func.attr in ("get", "post", "patch", "delete", "put", "tool")
                       for d in fn.decorator_list)
            if door:
                yield f"korely_agent/{rel}", fn.name, names(fn, {fn.name})


def test_every_door_asks_the_keys_end_users():
    doors = list(_doors())
    assert len(doors) > 20, doors
    blind = {(rel, fn) for rel, fn, used in doors if not used & _ASKS} - _NEED_NOT
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
