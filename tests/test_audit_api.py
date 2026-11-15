"""`GET /v1/audit` — the endpoint a buyer runs before they look at the product.

Which is why it was the last one without tests. The trail is the answer to
"who read this, and when", and an access or erasure request arrives as exactly
one of these queries. Two ways it can be quietly wrong: showing somebody else's
events, and a `total` that does not match what was filtered. The first is the
worst thing in the service; the second is how an export silently loses rows.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AuditEvent, Tenant
from tests.conftest import seeded_project_id
from korely_agent.services import keys as key_service


NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


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


def event(db, tenant_id, action="read", *, eu="u1", ts=NOW, result="ok",
          target=None, meta=None, ip=None, actor="rest"):
    # Filed under the account's default project, where `auth`'s key lives: a
    # key reads its own project's trail (tests/test_v1_project_wall_audit.py),
    # so a row seeded by hand needs the project a real call would stamp.
    row = AuditEvent(id=uuid.uuid4(), tenant_id=tenant_id, ts=ts, actor=actor,
                     action=action, result=result, end_user_id=eu,
                     target_id=target, meta=meta, ip=ip,
                     project_id=seeded_project_id(db, tenant_id))
    db.add(row)
    db.commit()
    return row


class TestIsolation:
    def test_one_tenant_never_sees_anothers_trail(self, db, tenant, client, auth):
        other = Tenant(id=uuid.uuid4(), name="Somebody Else")
        db.add(other)
        db.commit()

        event(db, tenant.id, "read", eu="mine")
        event(db, other.id, "read", eu="theirs")
        event(db, other.id, "erase", eu="theirs")

        r = client.get("/v1/audit", headers=auth)
        assert r.status_code == 200
        body = r.json()

        users = {e["user_id"] for e in body["events"]}
        assert users == {"mine"}, "the audit trail leaked across tenants"
        assert body["total"] == 1, "total counted rows the caller cannot see"

    def test_no_key_no_trail(self, db, tenant, client):
        event(db, tenant.id)
        assert client.get("/v1/audit").status_code in (401, 403)


class TestFilters:
    def test_by_end_user(self, db, tenant, client, auth):
        event(db, tenant.id, eu="alice")
        event(db, tenant.id, eu="bob")

        r = client.get("/v1/audit", params={"user_id": "alice"}, headers=auth).json()
        assert r["total"] == 1
        assert r["events"][0]["user_id"] == "alice"

    def test_by_action(self, db, tenant, client, auth):
        event(db, tenant.id, "read")
        event(db, tenant.id, "erase")
        event(db, tenant.id, "erase")

        r = client.get("/v1/audit", params={"action": "erase"}, headers=auth).json()
        assert r["total"] == 2
        assert {e["action"] for e in r["events"]} == {"erase"}

    def test_the_window_includes_both_ends(self, db, tenant, client, auth):
        """`since` and `until` are both inclusive. An exclusive end drops the
        last day of a month asked for by date, which is the shape of every
        request that ever arrives."""
        event(db, tenant.id, eu="before", ts=NOW - timedelta(days=2))
        event(db, tenant.id, eu="on-since", ts=NOW - timedelta(days=1))
        event(db, tenant.id, eu="inside", ts=NOW)
        event(db, tenant.id, eu="on-until", ts=NOW + timedelta(days=1))
        event(db, tenant.id, eu="after", ts=NOW + timedelta(days=2))

        r = client.get("/v1/audit", headers=auth, params={
            "since": (NOW - timedelta(days=1)).isoformat(),
            "until": (NOW + timedelta(days=1)).isoformat(),
        }).json()

        assert {e["user_id"] for e in r["events"]} == {"on-since", "inside", "on-until"}
        assert r["total"] == 3

    def test_a_moment_with_no_offset_is_utc_whatever_the_databases_zone(
            self, db, tenant, client, auth):
        """`since` and `until` were typed datetimes, and one with no offset
        stayed naive: Postgres compares it with `ts` in the session's time
        zone, so on a database in Europe/Rome `since=12:00` meant 10:00 UTC
        while `as_of` on /v1/facts read the same string as 12:00 UTC (audit
        of 17 Oct 2026). Both doors of the trail read it as `as_of` is read."""
        from sqlalchemy import text

        _row, full = key_service.create_api_key(
            db, tenant_id=tenant.id, name="admin",
            scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
        admin = {"Authorization": f"Bearer {full}"}
        event(db, tenant.id, eu="at-11:30Z", ts=NOW - timedelta(minutes=30))
        event(db, tenant.id, eu="at-12:30Z", ts=NOW + timedelta(minutes=30))
        naive = {"since": "2026-06-15T12:00", "until": "2026-06-15T13:00"}
        # The database's zone and not one session's: the requests and the
        # rest of the test take whichever pooled connection is free, and a
        # SET on one of them stayed behind for the tests after, on a pool of
        # two (tests/test_common_engine_writing.py read its dates in Rome).
        name = db.execute(text("SELECT current_database()")).scalar_one()

        def zone(sql):
            db.execute(text(f'ALTER DATABASE "{name}" {sql}'))
            db.commit()
            db.get_bind().dispose()

        try:
            zone("SET timezone = 'Europe/Rome'")
            for path, head in (("/v1/audit", auth), ("/admin/audit", admin)):
                got = client.get(path, headers=head, params=naive).json()["events"]
                assert [e["user_id"] for e in got] == ["at-12:30Z"], path
            by_date = client.get("/v1/audit", headers=auth,
                                 params={"since": "2026-06-15", "until": "2026-06-15T12:00"})
            assert [e["user_id"] for e in by_date.json()["events"]] == ["at-11:30Z"]
            bad = client.get("/v1/audit", headers=auth, params={"since": "yesterday"})
            assert bad.status_code == 422 and "since" in bad.json()["message"]
        finally:
            db.rollback()
            zone("RESET timezone")

    def test_filters_combine(self, db, tenant, client, auth):
        event(db, tenant.id, "read", eu="alice")
        event(db, tenant.id, "erase", eu="alice")
        event(db, tenant.id, "erase", eu="bob")

        r = client.get("/v1/audit", headers=auth,
                       params={"user_id": "alice", "action": "erase"}).json()
        assert r["total"] == 1
        assert r["events"][0]["user_id"] == "alice"
        assert r["events"][0]["action"] == "erase"


class TestPaging:
    def test_newest_first(self, db, tenant, client, auth):
        event(db, tenant.id, eu="oldest", ts=NOW - timedelta(hours=2))
        event(db, tenant.id, eu="newest", ts=NOW)
        event(db, tenant.id, eu="middle", ts=NOW - timedelta(hours=1))

        got = [e["user_id"] for e in client.get("/v1/audit", headers=auth).json()["events"]]
        assert got == ["newest", "middle", "oldest"]

    def test_total_is_the_whole_match_not_the_page(self, db, tenant, client, auth):
        """A `total` that reports the page length makes an export stop early
        and look complete."""
        for i in range(5):
            event(db, tenant.id, eu=f"u{i}", ts=NOW - timedelta(minutes=i))

        r = client.get("/v1/audit", headers=auth, params={"limit": 2}).json()
        assert len(r["events"]) == 2
        assert r["total"] == 5

    def test_offset_walks_without_repeating(self, db, tenant, client, auth):
        for i in range(5):
            event(db, tenant.id, eu=f"u{i}", ts=NOW - timedelta(minutes=i))

        # Up to NOW: each read of the trail is itself a row of it, stamped
        # with the real clock (after the seeded ones), and a walk over the
        # whole trail would meet the first page's own read on the second.
        first = client.get("/v1/audit", headers=auth,
                           params={"limit": 2, "until": NOW.isoformat()}).json()["events"]
        second = client.get("/v1/audit", headers=auth,
                            params={"limit": 2, "offset": 2,
                                    "until": NOW.isoformat()}).json()["events"]

        ids = [e["user_id"] for e in first] + [e["user_id"] for e in second]
        assert ids == ["u0", "u1", "u2", "u3"]
        assert len(set(ids)) == 4

    def test_offset_walks_ties_without_repeating(self, db, tenant, client, auth):
        """The events of one transaction share its timestamp to the
        microsecond. Ordered by time alone, an offset over them repeats some
        and skips others; the id decides among ties, as in the hosted one."""
        rows = [event(db, tenant.id, eu=f"u{i}", ts=NOW) for i in range(6)]

        walked = []
        for offset in range(0, 6, 2):
            page = client.get("/v1/audit", headers=auth,
                              params={"limit": 2, "offset": offset,
                                      "until": NOW.isoformat()}).json()["events"]
            walked += [e["user_id"] for e in page]

        by_id = sorted(rows, key=lambda r: r.id, reverse=True)
        assert walked == [r.end_user_id for r in by_id]

    def test_an_empty_filter_is_refused_not_read_as_none(self, db, tenant, client, auth):
        """`user_id=` read as "no filter" and listed every end user's events
        (found by the SDK's contract check, 2026-10-06)."""
        event(db, tenant.id, eu="u1")
        for params in ({"user_id": ""}, {"action": ""}):
            assert client.get("/v1/audit", headers=auth, params=params).status_code == 422

    def test_limits_are_bounded(self, client, auth):
        assert client.get("/v1/audit", headers=auth, params={"limit": 0}).status_code == 422
        assert client.get("/v1/audit", headers=auth, params={"limit": 5000}).status_code == 422
        assert client.get("/v1/audit", headers=auth, params={"offset": -1}).status_code == 422


class TestWhatARowCarries:
    def test_the_fields_a_report_needs_survive_the_round_trip(self, db, tenant, client, auth):
        event(db, tenant.id, "erase", eu="alice", result="ok",
              target="mem_deadbeef", meta={"memories": 3}, ip="203.0.113.7",
              actor="manage")

        row = client.get("/v1/audit", headers=auth).json()["events"][0]
        assert row["action"] == "erase"
        assert row["actor"] == "manage"
        assert row["result"] == "ok"
        assert row["user_id"] == "alice"
        assert row["target_id"] == "mem_deadbeef"
        assert row["meta"] == {"memories": 3}
        assert row["ip"] == "203.0.113.7"

    def test_an_empty_trail_is_an_empty_page_not_an_error(self, client, auth):
        r = client.get("/v1/audit", headers=auth)
        assert r.status_code == 200
        assert r.json() == {"events": [], "total": 0}

    def test_a_refused_attempt_is_visible(self, db, tenant, client, auth):
        """The rows worth having are the ones where somebody tried and failed:
        a real refusal, a key confined to Bob asking for Alice's memories,
        is `error` with its token in `meta.code` (audit of 27 Oct 2026: this
        seeded a `denied` row by hand, a result nothing writes)."""
        from korely_agent.services import keys as key_service

        _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name="bob's",
                                              scopes=["memories:read"], end_users=["bob"])
        refused = client.get("/v1/memories", params={"user_id": "alice"},
                             headers={"Authorization": f"Bearer {full}"})
        assert refused.status_code == 403
        rows = client.get("/v1/audit", headers=auth, params={"user_id": "alice"}).json()["events"]
        assert [(r["result"], r["meta"]["code"]) for r in rows] == [("error", "forbidden")]
