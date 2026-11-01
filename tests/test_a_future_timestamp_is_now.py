"""A memory's timestamp later than its write is read as the write (2026-10-07).

memory_core 3.1.5 (`worker.read_event_time`), from a
new-customer test of the hosted product: a client clock three minutes ahead
wrote memories whose facts began in the future, so every read of the present
missed them (/v1/context empty), and a 2030 timestamp hid them until 2030.
The choice of 6 October, among a 422, now and leaving it: never a
422. Every door of this product reads the timestamp through that one function
(REST, each item of a batch, the MCP), so the rule holds here by itself; this
is the REST door, end to end.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.db.models import AgentMemory
from korely_agent.services import keys as key_service



@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient
    from korely_agent import config
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    config.get_settings.cache_clear()
    try:
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()
    finally:
        config.get_settings.cache_clear()


@pytest.fixture
def head(db, tenant):
    _row, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}",
        scopes=["memories:read", "memories:write"])
    return {"Authorization": f"Bearer {full}"}


def stored_time(client, head, db, timestamp):
    before = datetime.now(timezone.utc)
    r = client.post("/v1/memories", headers=head, json={
        "content": "Maria moved to Rome.", "user_id": "maria", "timestamp": timestamp})
    assert r.status_code == 201, r.text
    after = datetime.now(timezone.utc)
    db.expire_all()
    row = db.query(AgentMemory).order_by(AgentMemory.created_at.desc(), AgentMemory.id.desc()).first()
    return before, row.event_time, after


@pytest.mark.parametrize("ahead", [timedelta(minutes=3), timedelta(days=365 * 4)])
def test_a_time_ahead_is_read_as_the_write(client, head, db, ahead):
    before, got, after = stored_time(client, head, db, (datetime.now(timezone.utc) + ahead).isoformat())
    assert before <= got <= after, (before, got, after)


def test_a_time_in_the_past_is_kept(client, head, db):
    _b, got, _a = stored_time(client, head, db, "2023-05-08T13:56:00Z")
    assert got == datetime(2023, 5, 8, 13, 56, tzinfo=timezone.utc)
