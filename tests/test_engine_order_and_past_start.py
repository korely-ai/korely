"""Two fixes of the audit of 27 Oct 2026, memory_core 5.12.0.

Memories with the same event time (a date-only or a repeated
`timestamp`: the import path of a dated history) were claimed and mined in
the order of their random uuid4 id, so "I moved to Paris" could reach the
judge before "I live in Rome" and the wrong fact stayed current. The order of
writing breaks the tie (Graphiti's T', Rasmussen et al., 2025, section 2.2.3).

A past fact written with POST /v1/facts and no `valid_from` was born
closed before it began (invalid_at the engine's clock, valid_from the column's
default read later at the flush). It begins when it was said.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import keys as key_service
from korely_agent.services.facts import drain
from korely_agent.services.memory_core import worker as lav

DAY = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _written_in_order(db, tenant, n, *, end_user="maria"):
    """n pending memories of one end user, one event time, created a minute
    apart, ids in the REVERSE order of writing (what a random uuid4 can give)."""
    ids = sorted(uuid.UUID(int=(i + 1) << 100) for i in range(n))[::-1]
    out = []
    for i, mid in enumerate(ids):
        m = AgentMemory(id=mid, tenant_id=tenant.id, end_user_id=end_user, content=f"statement {i}",
                        graph_sync_status="pending", event_time=DAY,
                        created_at=DAY + timedelta(minutes=i + 1))
        db.add(m)
        out.append(m)
    db.commit()
    return out


def test_the_claim_takes_same_instant_memories_in_the_order_they_were_written(db, tenant):
    written = _written_in_order(db, tenant, 5)
    took = lav.claim_pending(db, AgentMemory, limit=10, expiry_minutes=30)
    assert [m.content for m in took] == [m.content for m in written]


def test_the_claim_cap_of_a_person_keeps_the_earliest_written(db, tenant):
    written = _written_in_order(db, tenant, 5)
    took = lav.claim_pending(db, AgentMemory, limit=10, expiry_minutes=30,
                      person=AgentMemory.end_user_id, per_person=2)
    assert [m.content for m in took] == [written[0].content, written[1].content]


def test_a_group_is_mined_in_the_order_of_writing_when_the_event_time_ties():
    first = NS(id=uuid.UUID(int=1 << 120), event_time=DAY, created_at=DAY + timedelta(minutes=1))
    second = NS(id=uuid.UUID(int=1), event_time=DAY, created_at=DAY + timedelta(minutes=2))
    assert sorted([second, first], key=drain._memory_order_key) == [first, second]
    # the event time still decides first
    earlier = NS(id=uuid.UUID(int=5), event_time=DAY - timedelta(days=1), created_at=DAY + timedelta(minutes=9))
    assert sorted([first, earlier], key=drain._memory_order_key) == [earlier, first]


def test_the_share_of_an_account_ranks_same_instant_memories_by_writing(db, tenant):
    written = _written_in_order(db, tenant, 4)
    # a second account makes the share apply (drain._each_account_its_share)
    from korely_agent.db.models import Tenant
    other = Tenant(id=uuid.uuid4(), name="other")
    db.add(other)
    db.commit()
    db.add(AgentMemory(id=uuid.uuid4(), tenant_id=other.id, end_user_id="bob", content="b",
                       graph_sync_status="pending", event_time=DAY, created_at=DAY))
    db.commit()
    condition = drain._each_account_its_share(db, 2, None)
    assert condition is not None
    mine = {i for (i,) in db.query(AgentMemory.id).filter(condition, AgentMemory.tenant_id == tenant.id)}
    assert mine == {written[0].id}, "the account's share is the first written, not the smallest id"


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


def test_a_past_fact_with_no_start_is_not_born_closed(client, head, db):
    r = client.post("/v1/facts", headers=head, json={
        "subject": "maria", "predicate": "lives_in", "object": "Turin", "user_id": "u1", "tense": "past"})
    assert r.status_code == 201, r.text
    body = r.json()
    start = datetime.fromisoformat(body["valid_from"])
    end = datetime.fromisoformat(body["invalid_at"])
    assert start <= end, (body["valid_from"], body["invalid_at"])
    db.expire_all()
    row = db.query(AgentFact).filter(AgentFact.subject_label == "maria").one()
    assert row.valid_from <= row.invalid_at


def test_a_past_fact_with_a_start_keeps_it(client, head, db):
    r = client.post("/v1/facts", headers=head, json={
        "subject": "maria", "predicate": "lives_in", "object": "Rome", "user_id": "u1", "tense": "past",
        "valid_from": "2020-01-01T00:00:00Z"})
    assert r.status_code == 201, r.text
    assert r.json()["valid_from"].startswith("2020-01-01")
