"""`as_of`, which is the thing the product is for.

Everything else here is a list endpoint. This one claims to answer what the
store believed on a past date, and the ways that can be quietly wrong are all
boundary conditions: a fact that started the day after, a fact that ended the
day before, a fact that is closed now and was open then. Each of those is one
comparison operator, and each operator has a wrong version that looks right on
the happy path.
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentFact, Tenant
from korely_agent.services import keys as key_service


D = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


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


def put(db, tenant_id, subject, predicate, obj, since, until=None, eu="u1"):
    from tests.conftest import seeded_project_id
    db.add(AgentFact(
        id=uuid.uuid4(), tenant_id=tenant_id, end_user_id=eu,
        project_id=seeded_project_id(db, tenant_id),
        subject_label=subject, predicate_raw=predicate, predicate_canonical=predicate,
        object_label=obj, valid_from=D(since), invalid_at=D(until) if until else None,
        confidence=0.9,
    ))
    db.commit()


@pytest.fixture
def timeline(db, tenant):
    """One relation, three states, so every boundary has a fact on each side."""
    put(db, tenant.id, "c1", "is_on", "Starter", "2026-01-10", "2026-03-15")
    put(db, tenant.id, "c1", "is_on", "Business", "2026-03-15", "2026-07-11")
    put(db, tenant.id, "c1", "is_on", "Starter again", "2026-07-11")
    put(db, tenant.id, "c1", "lives_in", "Milan", "2026-02-01")
    return tenant


def objects(r):
    return sorted(f["object"] for f in r.json()["facts"])


class TestAsOf:
    @pytest.mark.parametrize("when,expected", [
        ("2026-01-01", []),                               # before anything began
        ("2026-02-01", ["Milan", "Starter"]),
        ("2026-05-01", ["Business", "Milan"]),
        ("2026-08-01", ["Milan", "Starter again"]),
        ("2099-01-01", ["Milan", "Starter again"]),       # far future = now
    ])
    def test_the_state_on_a_date(self, client, auth, timeline, when, expected):
        r = client.get("/v1/facts", headers=auth,
                       params={"user_id": "u1", "as_of": when})
        assert r.status_code == 200, r.text
        assert objects(r) == expected

    def test_the_day_a_fact_starts_it_is_already_true(self, client, auth, timeline):
        """valid_from is inclusive. Exclusive would leave a one-day hole in
        which the store believes nothing, on the exact day it learned."""
        r = client.get("/v1/facts", headers=auth,
                       params={"user_id": "u1", "as_of": "2026-03-15"})
        assert "Business" in objects(r)

    def test_the_day_a_fact_ends_it_is_no_longer_true(self, client, auth, timeline):
        """invalid_at is exclusive, and it must be: the replacement starts the
        same instant, and both being true is the contradiction the store exists
        to avoid."""
        r = client.get("/v1/facts", headers=auth,
                       params={"user_id": "u1", "as_of": "2026-03-15"})
        assert "Starter" not in objects(r)
        assert len([o for o in objects(r) if "Starter" in o or "Business" in o]) == 1

    def test_without_as_of_only_what_is_true_now(self, client, auth, timeline):
        r = client.get("/v1/facts", headers=auth, params={"user_id": "u1"})
        assert objects(r) == ["Milan", "Starter again"]

    def test_include_invalidated_shows_the_whole_history(self, client, auth, timeline):
        r = client.get("/v1/facts", headers=auth,
                       params={"user_id": "u1", "include_invalidated": "true"})
        assert len(objects(r)) == 4

    @pytest.mark.parametrize("bad", ["yesterday", "2026-13-45", "1709294400",
                                     "2026-05-01T25:00:00Z", "01/03/2026"])
    def test_an_unreadable_date_is_refused(self, client, auth, timeline, bad):
        r = client.get("/v1/facts", headers=auth,
                       params={"user_id": "u1", "as_of": bad})
        assert r.status_code == 422, (
            f"{bad!r} was accepted; the answer would be about today while the "
            f"caller asked about something else"
        )

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_date_means_no_point_in_time_query(self, client, auth,
                                                       timeline, blank):
        """`as_of=` is how several HTTP clients serialize "no value", and
        refusing "" while accepting "  " is a difference nobody can predict."""
        r = client.get("/v1/facts", headers=auth,
                       params={"user_id": "u1", "as_of": blank})
        assert r.status_code == 200
        assert objects(r) == ["Milan", "Starter again"]

    def test_a_date_and_a_datetime_agree(self, client, auth, timeline):
        a = client.get("/v1/facts", headers=auth,
                       params={"user_id": "u1", "as_of": "2026-05-01"})
        b = client.get("/v1/facts", headers=auth,
                       params={"user_id": "u1", "as_of": "2026-05-01T00:00:00Z"})
        assert objects(a) == objects(b)


class TestFilters:
    def test_entity_matches_either_side(self, client, auth, db, tenant):
        put(db, tenant.id, "Marco", "works_at", "Acme", "2026-01-01")
        for value, expected in (("Marco", 1), ("Acme", 1), ("Nobody", 0)):
            r = client.get("/v1/facts", headers=auth,
                           params={"user_id": "u1", "entity": value})
            assert len(r.json()["facts"]) == expected, value

    def test_subject_matches_only_the_subject(self, client, auth, db, tenant):
        put(db, tenant.id, "Marco", "works_at", "Acme", "2026-01-01")
        r = client.get("/v1/facts", headers=auth,
                       params={"user_id": "u1", "subject": "Acme"})
        assert r.json()["facts"] == []

    def test_filters_are_anded(self, client, auth, timeline):
        r = client.get("/v1/facts", headers=auth,
                       params={"user_id": "u1", "predicate": "lives_in",
                               "as_of": "2026-01-15"})
        assert r.json()["facts"] == [], "lives_in did not exist yet in January"

    def test_an_unknown_end_user_is_empty_not_everything(self, client, auth, timeline):
        r = client.get("/v1/facts", headers=auth, params={"user_id": "nobody"})
        assert r.json()["facts"] == []


class TestPagingReportsTheTruth:
    def test_total_counts_matches_not_the_page(self, client, auth, db, tenant):
        for i in range(7):
            put(db, tenant.id, "c1", "likes", f"thing{i}", "2026-01-01")
        r = client.get("/v1/facts", headers=auth,
                       params={"user_id": "u1", "limit": 3})
        body = r.json()
        assert len(body["facts"]) == 3
        assert body["total"] == 7

    def test_pages_do_not_overlap(self, client, auth, db, tenant):
        for i in range(7):
            put(db, tenant.id, "c1", "likes", f"thing{i}", "2026-01-01")
        a = client.get("/v1/facts", headers=auth,
                       params={"user_id": "u1", "limit": 3}).json()["facts"]
        b = client.get("/v1/facts", headers=auth,
                       params={"user_id": "u1", "limit": 3, "offset": 3}).json()["facts"]
        assert not ({f["id"] for f in a} & {f["id"] for f in b})


class TestIsolation:
    def test_a_past_date_is_not_a_way_around_the_tenant(self, client, auth, db,
                                                        tenant, timeline):
        other = Tenant(id=uuid.uuid4(), name="Other")
        db.add(other)
        db.commit()
        put(db, other.id, "c1", "is_on", "THEIRSECRET", "2026-01-01")

        for params in ({"user_id": "u1"},
                       {"user_id": "u1", "as_of": "2026-05-01"},
                       {"user_id": "u1", "include_invalidated": "true"},
                       {}):
            r = client.get("/v1/facts", headers=auth, params=params)
            assert "THEIRSECRET" not in r.text, params
