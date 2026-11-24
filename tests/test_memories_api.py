"""The main surface, asked the questions a client's first integration asks.

The bi-temporal store's whole claim is that it can answer what was true at a
date. That claim runs through one field, `timestamp`, and if a bad value there
is quietly replaced with "now" the claim fails silently: the write succeeds,
every `as_of` query about that period is wrong, and nothing anywhere says why.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentMemory, Tenant
from korely_agent.services import keys as key_service
from korely_agent.services.ids import parse_mem_id



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


@pytest.fixture
def auth(db, tenant):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="t")
    return {"Authorization": f"Bearer {full}"}


class TestTheEventTime:
    @pytest.mark.parametrize("ts", [
        "yesterday",
        "01/03/2026",
        "2026-13-45",
        "not a date at all",
        "1709294400",          # a unix timestamp is not ISO 8601
    ])
    def test_an_unreadable_timestamp_is_refused(self, client, auth, db, ts):
        r = client.post("/v1/memories", headers=auth,
                        json={"content": "Marco moved to Berlin", "user_id": "u1",
                              "timestamp": ts})
        assert r.status_code == 422, (
            f"{ts!r} was accepted; the memory now carries today's date and "
            f"every as_of query about it is wrong"
        )
        assert "ISO 8601" in r.text
        assert db.query(AgentMemory).count() == 0, "wrote it anyway"

    @pytest.mark.parametrize("ts,expected_day", [
        ("2026-03-01", "2026-03-01"),
        ("2026-03-01T14:30:00Z", "2026-03-01"),
        ("2026-03-01T14:30:00+01:00", "2026-03-01"),
    ])
    def test_a_readable_one_is_kept(self, client, auth, db, ts, expected_day):
        r = client.post("/v1/memories", headers=auth,
                        json={"content": "Marco moved to Berlin", "user_id": "u1",
                              "timestamp": ts})
        assert r.status_code == 201, r.text
        m = db.query(AgentMemory).one()
        # On the row, whichever write path ran (agent_memories.event_time);
        # the day in UTC, since the column holds the instant.
        from datetime import timezone

        assert m.event_time is not None
        assert m.event_time.astimezone(timezone.utc).date().isoformat() == expected_day
        assert "_event_time" not in (m.meta or {})

    def test_no_timestamp_is_not_an_error(self, client, auth):
        r = client.post("/v1/memories", headers=auth,
                        json={"content": "Marco likes fish", "user_id": "u1"})
        assert r.status_code == 201, r.text

    def test_the_worker_never_receives_a_raw_string(self, client, auth, db):
        """What the worker reads must already be parsed, because it reads it
        minutes later and is the wrong place to discover a bad value. It is a
        timestamptz column now, so it cannot be anything else; a date alone is
        midnight UTC."""
        from datetime import datetime, timezone

        client.post("/v1/memories", headers=auth,
                    json={"content": "x", "user_id": "u1",
                          "timestamp": "2026-03-01"})
        m = db.query(AgentMemory).one()
        assert m.event_time == datetime(2026, 3, 1, tzinfo=timezone.utc)


class TestContent:
    def test_empty_is_refused(self, client, auth):
        assert client.post("/v1/memories", headers=auth,
                           json={"content": "", "user_id": "u"}).status_code == 422

    def test_whitespace_only_is_refused(self, client, auth):
        r = client.post("/v1/memories", headers=auth,
                        json={"content": "   \n\t ", "user_id": "u"})
        assert r.status_code == 422
        assert "whitespace" in r.text

    def test_oversized_metadata_is_refused_not_truncated(self, client, auth, db):
        r = client.post("/v1/memories", headers=auth,
                        json={"content": "x", "user_id": "u",
                              "metadata": {"blob": "y" * 20000}})
        assert r.status_code == 422
        assert db.query(AgentMemory).count() == 0


class TestPublicShape:
    def test_the_response_says_user_id_never_tenant_id(self, client, auth):
        r = client.post("/v1/memories", headers=auth,
                        json={"content": "x", "user_id": "u1"})
        body = r.json()
        assert body["user_id"] == "u1"
        assert "tenant_id" not in body
        assert "end_user_id" not in body

    def test_ids_are_prefixed_and_round_trip(self, client, auth):
        r = client.post("/v1/memories", headers=auth,
                        json={"content": "x", "user_id": "u1"})
        mid = r.json()["id"]
        assert mid.startswith("mem_")
        assert parse_mem_id(mid) is not None
        assert client.get(f"/v1/memories/{mid}", headers=auth).status_code == 200

    def test_a_malformed_id_is_404_not_500(self, client, auth):
        # 404 only, with its code: this accepted 422 as well, which the name
        # does not promise and the route never answers (audit of 17 Oct 2026).
        for bad in ("mem_notahex", "banana", "mem_", "../../etc/passwd"):
            r = client.get(f"/v1/memories/{bad}", headers=auth)
            assert r.status_code == 404, f"{bad!r} -> {r.status_code}"
            assert r.json()["code"] == "not_found", f"{bad!r} -> {r.text}"

    def test_an_id_from_another_tenant_is_404(self, client, auth, db, tenant):
        other = Tenant(id=uuid.uuid4(), name="Other")
        db.add(other)
        db.commit()
        _r, other_key = key_service.create_api_key(db, tenant_id=other.id, name="o")

        mine = client.post("/v1/memories", headers=auth,
                           json={"content": "secret", "user_id": "u1"}).json()["id"]
        r = client.get(f"/v1/memories/{mine}",
                       headers={"Authorization": f"Bearer {other_key}"})
        assert r.status_code == 404


class TestTheOperatorOwnsTheCapacity:
    """The hosted service caps an account by its key's tier: 2 agent
    namespaces on `hobby`, 10 on `developer`, monthly write and query quotas.
    Every key minted here carries `tier = "hobby"`, the column's default,
    because nothing passes another. So if any of those limits still read the
    tier, every install would be held to the hosted free plan on its own
    hardware. None does (2026-09-28): the only ceiling is RATE_LIMIT_PER_MINUTE,
    off unless the operator sets it, counted per account and not per tier.
    This pins it."""

    AGENTS = [f"bot-{i}" for i in range(12)]   # past hobby's 2 and developer's 10

    def test_a_default_key_writes_under_as_many_agents_as_it_likes(self, client, auth, db):
        from korely_agent.db.models import AgentApiKey

        assert {k.tier for k in db.query(AgentApiKey).all()} == {"hobby"}, (
            "the premise: this is the key every install hands its agents")
        for i, agent in enumerate(self.AGENTS):
            if i % 2:
                r = client.post("/v1/facts", headers=auth, json={
                    "user_id": "u1", "agent_id": agent, "subject": "u1",
                    "predicate": "uses", "object": f"tool {i}"})
            else:
                r = client.post("/v1/memories", headers=auth, json={
                    "content": f"note {i}", "user_id": "u1", "agent_id": agent})
            assert r.status_code == 201, f"write {i + 1} under {agent}: {r.status_code} {r.text}"

        page = client.get("/v1/agents", headers=auth, params={"limit": 50}).json()
        assert page["used"] == len(self.AGENTS)
        assert page["cap"] == 0, "0 is 'no ceiling'; the field stays for the SDKs that read it"


class TestTheAgentCapIsTheOneWritesMeet:
    """`cap` and `used` in GET /v1/agents are what an SDK reads to know whether
    a new agent name will be accepted. On the Cloud the reply said cap 0 (no
    ceiling) and counted the key's project, while the writes were refused at
    the plan's ceiling counted over the whole account (audit of 17 Oct 2026).
    The reply now takes both numbers from the `agent_cap` hook, the one
    the service registers next to its refusal."""

    def test_the_reply_carries_the_ceiling_a_hook_declares(self, client, auth, db, tenant):
        from korely_agent.core import extensions

        client.post("/v1/memories", headers=auth, json={
            "content": "note", "user_id": "u1", "agent_id": "bot-a"})
        asked = []

        def agent_cap(db_, tenant_id):
            asked.append(tenant_id)
            return 2, 5
        extensions.register("agent_cap", agent_cap)
        try:
            page = client.get("/v1/agents", headers=auth).json()
        finally:
            extensions.reset()
        assert asked == [tenant.id]
        assert (page["cap"], page["used"]) == (2, 5)
        assert page["total"] == 1, "the listing is still the key's own"

    def test_with_no_hook_there_is_no_ceiling(self, client, auth):
        client.post("/v1/memories", headers=auth, json={
            "content": "note", "user_id": "u1", "agent_id": "bot-a"})
        page = client.get("/v1/agents", headers=auth).json()
        assert (page["cap"], page["used"]) == (0, 1)
