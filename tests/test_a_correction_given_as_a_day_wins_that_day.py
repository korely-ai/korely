"""A `valid_from` given as a day and no time wins that day (found on 7 Nov 2026).

`korely add-fact ... --valid-from 2026-11-06`, written at 21:08 to correct a
fact read at 20:56 that same day, was stored from midnight: the wrong fact,
begun "later", stayed true and the correction became history. Against a fact
that began on the same day, a start known only to the day is the same valid
time, and the newer information wins (Graphiti's tie rule, memory_core
`judge.close`). A fact of another day, and a start with a time, close as
before. No model: the judge is scripted to name every candidate.
"""
import re

import pytest
from fastapi.testclient import TestClient

from sqlalchemy import text

from korely_agent.core.llm import LLMResult
from korely_agent.db.models import AgentFact
from korely_agent.services import keys as key_service
from korely_agent.services.facts.temporal import is_a_day
from tests.conftest import is_the_judge, judge_reply


class Model:
    def complete(self, messages, **kw):
        prompt = messages[0]["content"]
        if is_the_judge(prompt):
            return LLMResult(text=judge_reply(prompt, lambda text: [
                {"existing_id": i, "reason": "corrected"} for i in re.findall(r"id=([0-9a-f-]{36})", text)]))
        if '"family"' in prompt:
            return LLMResult(text='{"family": "other"}')
        if "many_values" in prompt:
            return LLMResult(text='{"many_values": false}')
        return LLMResult(text='{"same_as": null}')


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: Model())
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def auth(db, tenant):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="day")
    return {"Authorization": f"Bearer {full}"}


def _post(client, auth, obj, valid_from):
    r = client.post("/v1/facts", headers=auth, json={
        "user_id": "robin", "subject": "Robin", "predicate": "also_known_as", "object": obj,
        "valid_from": valid_from})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _open(db):
    db.expire_all()
    return sorted(f.object_label for f in db.query(AgentFact).filter(AgentFact.invalid_at.is_(None)).all())


def test_is_a_day():
    assert is_a_day("2026-11-06") and is_a_day(" 2026-11-06 ")
    assert not is_a_day("2026-11-06T21:08:00Z") and not is_a_day("2026-11-06 21:08") and not is_a_day("")


def test_a_correction_given_as_a_day_closes_the_fact_begun_later_that_day(client, auth, db):
    _post(client, auth, "the lead", "2026-11-06T20:56:00Z")
    _post(client, auth, "the tester", "2026-11-06")
    assert _open(db) == ["the tester"]
    wrong = db.query(AgentFact).filter(AgentFact.object_label == "the lead").one()
    assert wrong.invalid_at == wrong.valid_from, "closed where it began: the same valid time, the newer word wins"


def test_a_correction_with_a_time_still_loses_to_a_later_start(client, auth, db):
    _post(client, auth, "the lead", "2026-11-06T20:56:00Z")
    _post(client, auth, "the tester", "2026-11-06T00:00:00Z")
    assert _open(db) == ["the lead"], "a time was given: the fact that began later stays, as before"


def test_a_day_before_the_fact_still_loses(client, auth, db):
    _post(client, auth, "the lead", "2026-11-07T09:00:00Z")
    _post(client, auth, "the tester", "2026-11-06")
    assert _open(db) == ["the lead"], "another day: the later fact stays, as before"


@pytest.fixture
def session_tz(db, request):
    """The database's time zone for one test, as tests/test_audit_api.py moves
    it: the requests take whichever pooled connection is free, and a SET on
    one session would stay on the pool (tests/conftest.py's guard)."""
    name = db.execute(text("SELECT current_database()")).scalar_one()

    def zone(sql):
        db.execute(text(f'ALTER DATABASE "{name}" {sql}'))
        db.commit()
        db.get_bind().dispose()

    zone(f"SET timezone = '{request.param}'")
    try:
        yield request.param
    finally:
        db.rollback()
        zone("RESET timezone")


@pytest.mark.parametrize("session_tz", ["UTC", "Asia/Tokyo", "America/New_York"], indirect=True)
def test_the_same_utc_day_wins_whatever_the_sessions_time_zone(client, auth, db, session_tz):
    """The review of 25 Nov 2026: a valid_from read back from Postgres comes in the
    session's time zone; 20:56Z on 6 Nov is 05:56 on 7 Nov in Tokyo, and the
    correction lost there. The two are compared as UTC days."""
    _post(client, auth, "the lead", "2026-11-06T20:56:00Z")
    db.expire_all()  # read back from Postgres, as a new request would
    _post(client, auth, "the tester", "2026-11-06")
    assert _open(db) == ["the tester"], session_tz


@pytest.mark.parametrize("session_tz", ["UTC", "Asia/Tokyo", "America/New_York"], indirect=True)
def test_the_next_utc_day_stays_whatever_the_sessions_time_zone(client, auth, db, session_tz):
    """02:00Z on 7 Nov is 21:00 on 6 Nov in New York, and a fact of the next
    UTC day was closed there. Another UTC day closes as before: it stays."""
    _post(client, auth, "the reviewer", "2026-11-07T02:00:00Z")
    db.expire_all()
    _post(client, auth, "the tester", "2026-11-06")
    assert _open(db) == ["the reviewer"], session_tz


def test_a_past_statement_given_as_a_day_is_judged_as_before(monkeypatch, client, auth, db):
    """The day rule is for a fact that holds from its day (`not g.past` in
    facts/service._judge_of_the_write): a past statement given as a day is
    judged as before 1.0.40."""
    from korely_agent.services.facts import contradiction

    seen = []
    real = contradiction.detect_and_invalidate

    def spy(*a, **k):
        seen.append((k.get("new_past"), k.get("new_valid_from_is_a_day")))
        return real(*a, **k)

    monkeypatch.setattr(contradiction, "detect_and_invalidate", spy)
    monkeypatch.setattr("korely_agent.services.facts.service.detect_and_invalidate", spy, raising=False)
    _post(client, auth, "the lead", "2026-11-06T20:56:00Z")
    r = client.post("/v1/facts", headers=auth, json={
        "user_id": "robin", "subject": "Robin", "predicate": "also_known_as", "object": "the tester",
        "valid_from": "2026-11-06", "tense": "past"})
    assert r.status_code == 201, r.text
    assert any(past for past, _day in seen), seen
    assert all(day is False for past, day in seen if past), seen
