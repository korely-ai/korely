"""GET /v1/memories/{id} shows the facts true now.

It showed every fact the memory ever asserted, superseded ones included, so a
read that looks like the current state had "Maria lives in Rome" next to
"Maria lives in Milan". The hosted service's audit against its own docs found
it (2026-09-29), and both engines now answer the same way, the one the SDK
reads: what is true now here, what it used to say under /history.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service
from korely_agent.services.ids import mem_public_id

NOW = datetime.now(timezone.utc)


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


def test_superseded_facts_are_history_not_state(client, db, tenant):
    default = project_service.get_or_create_default_project(db, tenant.id)
    _row, key = key_service.create_api_key(db, tenant_id=tenant.id, name="t")
    auth = {"Authorization": f"Bearer {key}"}
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=default.id,
                    content="Maria lives in Milan, she left Rome.", end_user_id="maria",
                    graph_sync_status="done")
    db.add(m)
    db.commit()

    def fact(obj, invalid_at=None):
        db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=default.id,
                         end_user_id="maria", subject_label="maria", subject_canonical="maria",
                         predicate_raw="lives_in", predicate_canonical="lives_in",
                         object_label=obj, object_canonical=obj, confidence=0.9,
                         valid_from=NOW - timedelta(days=100), invalid_at=invalid_at,
                         source_memory_id=m.id, source_memory_ids=[str(m.id)]))

    fact("Rome", invalid_at=NOW - timedelta(days=10))
    fact("Milan")
    fact("Turin", invalid_at=NOW + timedelta(days=30))   # ends later: still true today
    db.commit()

    now = client.get(f"/v1/memories/{mem_public_id(m.id)}", headers=auth).json()
    assert sorted(f["object"] for f in now["facts"]) == ["Milan", "Turin"]

    history = client.get(f"/v1/memories/{mem_public_id(m.id)}/history", headers=auth).json()
    assert "Rome" in str(history), "what the memory used to assert is still there, as history"
