"""GET /v1/users lists every end user the project still holds memories or
facts of, live or not (review of 1 Nov 2026).

An end user whose memories were all forgotten, or whose facts are all
closed, still has their text in the store: DELETE /v1/users/{id}/memories
erases it and counts it. The listing read live rows only, so such a person
was missing from it, and a queue of erasures built from the listing skipped
the people it existed for. Now they are listed with zero live counts, as
GET /v1/agents lists an agent whose data was all forgotten, until their
erasure takes them out.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import keys as key_service
from tests.conftest import seeded_project_id

PAST = datetime(2026, 3, 1, tzinfo=timezone.utc)


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


def key(db, tenant, **kw):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name=uuid.uuid4().hex[:8], **kw)
    return {"Authorization": f"Bearer {full}"}


def memory(db, tenant, end_user, *, forgotten=False, agent=None):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=end_user,
                    project_id=seeded_project_id(db, tenant.id), agent_id=agent,
                    content=f"a memory of {end_user}",
                    deleted_at=PAST if forgotten else None)
    db.add(m)
    db.commit()
    return m


def fact(db, tenant, end_user, *, closed=False):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=end_user,
                  project_id=seeded_project_id(db, tenant.id),
                  subject_label=end_user, predicate_raw="is on",
                  predicate_canonical="is_on", object_label="the Basic plan",
                  valid_from=PAST - timedelta(days=30),
                  invalid_at=PAST if closed else None)
    db.add(f)
    db.commit()
    return f


@pytest.fixture
def held(db, tenant):
    """Three end users: one with live data, one with only a forgotten
    memory, one with only a closed fact."""
    memory(db, tenant, "live")
    fact(db, tenant, "live")
    memory(db, tenant, "forgotten-only", forgotten=True)
    fact(db, tenant, "closed-only", closed=True)


def users(client, head, **params):
    r = client.get("/v1/users", headers=head, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_an_end_user_with_only_forgotten_or_closed_rows_is_listed(db, tenant, client, held):
    body = users(client, key(db, tenant))
    by_id = {u["user_id"]: u for u in body["users"]}
    assert set(by_id) == {"live", "forgotten-only", "closed-only"}
    assert body["total"] == 3
    # The counts stay those of what is live, as before.
    assert (by_id["live"]["memories"], by_id["live"]["facts"]) == (1, 1)
    for gone in ("forgotten-only", "closed-only"):
        assert (by_id[gone]["memories"], by_id[gone]["facts"]) == (0, 0)
        assert by_id[gone]["last_active"] is None
    # The live end user first: the most recently active first, nothing live last.
    assert body["users"][0]["user_id"] == "live"


def test_their_erasure_takes_them_out_of_the_listing(db, tenant, client, held):
    head = key(db, tenant)
    for gone in ("forgotten-only", "closed-only"):
        r = client.delete(f"/v1/users/{gone}/memories", headers=head)
        assert r.status_code == 200, r.text
    body = users(client, head)
    assert [u["user_id"] for u in body["users"]] == ["live"] and body["total"] == 1


def test_a_confined_key_lists_only_its_own_end_users(db, tenant, client, held):
    body = users(client, key(db, tenant, end_users=["forgotten-only"]))
    assert [u["user_id"] for u in body["users"]] == ["forgotten-only"]
    assert body["total"] == 1


def test_the_agent_filter_holds_for_the_rows_not_live(db, tenant, client):
    memory(db, tenant, "anna", forgotten=True, agent="support-bot")
    memory(db, tenant, "bruno", forgotten=True, agent="sales-bot")
    body = users(client, key(db, tenant), agent_id="support-bot")
    assert [u["user_id"] for u in body["users"]] == ["anna"] and body["total"] == 1


# ── Round 2 (review of 14 Nov 2026): every place that holds an end user ─────

def alias(db, tenant, end_user, agent=None):
    from korely_agent.db.models import AgentEntityAlias

    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                            end_user_id=end_user, agent_id=agent, alias_lower="anna",
                            alias_label="Anna", canonical_label=end_user))
    db.commit()


def test_an_end_user_held_only_in_another_place_is_listed(db, tenant, client):
    """A name declared and nothing else: the erasure takes it, so the list
    names its end user (services/end_user_places.held_selects)."""
    alias(db, tenant, "named-only")
    body = users(client, key(db, tenant))
    assert [(u["user_id"], u["memories"], u["facts"], u["last_active"]) for u in body["users"]] \
        == [("named-only", 0, 0, None)]
    assert body["total"] == 1
    assert client.delete("/v1/users/named-only/memories", headers=key(db, tenant)).status_code == 200
    assert users(client, key(db, tenant))["total"] == 0


def test_the_walls_hold_on_every_place(db, tenant, client):
    from korely_agent.db.models import AgentForgetAudit
    from korely_agent.services import projects

    alias(db, tenant, "mine", agent="support-bot")
    alias(db, tenant, "theirs", agent="sales-bot")
    other = projects.create_project(db, tenant_id=tenant.id, name="Other")
    from korely_agent.db.models import AgentEntityAlias

    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=other.id, end_user_id="elsewhere",
                            alias_lower="x", alias_label="X", canonical_label="x"))
    # What an erasure left: a pseudonym is nobody.
    db.add(AgentForgetAudit(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                            end_user_id="erased:" + "0" * 32, scope="user",
                            facts_invalidated=0))
    db.commit()
    assert {u["user_id"] for u in users(client, key(db, tenant))["users"]} == {"mine", "theirs"}
    assert [u["user_id"] for u in users(client, key(db, tenant), agent_id="support-bot")["users"]] \
        == ["mine"]
    assert [u["user_id"] for u in users(client, key(db, tenant, end_users=["theirs"]))["users"]] \
        == ["theirs"]


def test_every_place_that_holds_an_end_user_is_listed_or_says_why_not(db):
    from korely_agent.services import end_user_places as places

    for p in places.places_here(db):
        if p.holds and p.name not in places.NOT_LISTED:
            assert hasattr(p.find, "select"), f"{p.name}: no SQL form for GET /v1/users"
    assert set(places.NOT_LISTED) <= {p.name for p in places.PLACES if p.holds}


def test_the_pseudonym_shape_is_one_rule_in_python_and_sql(db):
    from sqlalchemy import literal, select

    from korely_agent.services.ids import is_pseudonym, pseudonym_condition

    for value in ("erased:" + "a" * 32, "erased:" + "A" * 32, "erased:" + "a" * 31,
                  "erased:anna", "anna", "xerased:" + "a" * 32, "erased:" + "a" * 33):
        sql = db.execute(select(pseudonym_condition(literal(value)))).scalar()
        assert bool(sql) is is_pseudonym(value), value


def test_a_confined_key_lists_the_agents_of_its_own_forgotten_rows(db, tenant, client):
    memory(db, tenant, "anna", forgotten=True, agent="support-bot")
    memory(db, tenant, "bruno", forgotten=True, agent="sales-bot")
    r = client.get("/v1/agents", headers=key(db, tenant, end_users=["anna"]))
    assert r.status_code == 200, r.text
    assert [(a["agent_id"], a["memories"]) for a in r.json()["agents"]] == [("support-bot", 0)]
    assert r.json()["total"] == 1
