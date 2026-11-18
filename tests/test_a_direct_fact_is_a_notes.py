"""A fact the developer writes directly is a note's, whether it is new or
states again a fact a work session stated (audit of 20 Oct 2026): the
reconfirmation kept `session_only`, so exclude_sessions=true hid the fact
just asserted, while the same POST for a new triple showed it. One helper
clears it (facts/service.stated_by_a_note), for a note memory and for a
direct write alike."""
import ast
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentFact

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_a_sessions_fact_stated_directly_shows_with_exclude_sessions(db, tenant, client):
    from korely_agent.services import keys as key_service

    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}",
                                          scopes=["memories:read", "memories:write"])
    head = {"Authorization": f"Bearer {full}"}
    body = {"subject": "proj-x", "predicate": "uses", "object": "Postgres", "user_id": "u1"}
    first = client.post("/v1/facts", headers=head, json=body)
    assert first.status_code == 201, first.text
    # As a session's extraction writes it (facts/service._persist_one).
    db.query(AgentFact).update({AgentFact.session_only: True})
    db.commit()
    again = client.post("/v1/facts", headers=head, json=body)
    assert again.status_code == 201 and again.json()["id"] == first.json()["id"], again.text
    r = client.get("/v1/facts", headers=head, params={"user_id": "u1", "exclude_sessions": "true"})
    assert r.json()["total"] == 1, r.text


@pytest.mark.parametrize("corrected_to", [None, "MySQL"])
def test_a_correction_is_a_direct_statement(db, tenant, client, corrected_to):
    """PATCH /v1/facts/{id} restating a session's fact (same triple), or
    correcting it to a new value, leaves a note's fact: exclude_sessions=true
    shows it (audit of 20 Oct 2026, the correction path)."""
    from korely_agent.services import keys as key_service

    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}",
                                          scopes=["memories:read", "memories:write"])
    head = {"Authorization": f"Bearer {full}"}
    body = {"subject": "proj-x", "predicate": "uses", "object": "Postgres", "user_id": "u1"}
    first = client.post("/v1/facts", headers=head, json=body)
    assert first.status_code == 201, first.text
    db.query(AgentFact).update({AgentFact.session_only: True})
    db.commit()
    r = client.patch(f"/v1/facts/{first.json()['id']}", headers=head,
                     json={"object": corrected_to or "Postgres"})
    assert r.status_code == 200, r.text
    shown = client.get("/v1/facts", headers=head, params={"user_id": "u1", "exclude_sessions": "true"})
    assert shown.json()["total"] == 1, shown.text


def test_session_only_is_cleared_in_one_place():
    """Only the helper writes the column false, outside memory_core: a third
    path that clears it by hand is the drift of this finding."""
    found = []
    for path in sorted((ROOT / "korely_agent").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "/memory_core/" in rel:
            continue
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                        and node.value.value is False
                        and any(isinstance(t, ast.Attribute) and t.attr == "session_only"
                                for t in node.targets)):
                    found.append((rel, fn.name))
    assert found == [("korely_agent/services/facts/service.py", "stated_by_a_note")], found
