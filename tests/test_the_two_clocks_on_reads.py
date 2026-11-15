"""Reads that keep the two clocks apart, and a correction that does not leave
two values true (audit of 18 Oct 2026).

CONTRIBUTING.md: when a fact was true (`valid_from`, `invalid_at`, read by
`as_of`) and when the store knew it (`created_at`, `closed_known_at`, read by
`known_as_of`; Graphiti, Rasmussen et al. 2025, section 2.2.3). A fact never
closes before it begins.

  * a fact read with `known_as_of` carried today's sources, count and
    last confirmation, memories written after the moment asked included;
  * a memory's history dated "fact extracted" by the fact's
    `valid_from` and "fact invalidated" by its `invalid_at`, the world's clock,
    so a memory written today about 2020 learned its fact six years before it
    existed, and an end set ahead showed as an event still to come;
  * PATCH of a fact closed on a date ahead wrote the correction and left
    the old value true until that date, while answering it superseded.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.core.llm import LLMResult
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import keys as key_service
from korely_agent.services.ids import fact_public_id, mem_public_id
from tests.conftest import in_the_judges_shape, is_the_judge, seeded_project_id


# When the module is read: every time below is relative to it, and a time
# compared with the clock reads the clock (the suite runs for most of an hour).
NOW = datetime.now(timezone.utc)


@pytest.fixture
def client(db, monkeypatch):
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
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}",
                                            scopes=["memories:read", "memories:write"])
    return {"Authorization": f"Bearer {full}"}


def _memory(db, tenant, written: datetime, text="Luca is on the Pro plan.") -> AgentMemory:
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                    end_user_id="luca", content=text, graph_sync_status="done", created_at=written)
    db.add(m)
    db.commit()
    return m


def _fact(db, tenant, *, learned: datetime, sources, valid_from: datetime,
          confirmed=None, invalid_at=None) -> AgentFact:
    f = AgentFact(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                  end_user_id="luca", subject_label="luca", subject_type="self",
                  predicate_raw="on_plan", predicate_canonical="on_plan", object_label="Pro plan",
                  object_is_literal=False, confidence=0.9, created_at=learned,
                  source_memory_id=sources[0].id if sources else None,
                  source_memory_ids=[str(m.id) for m in sources] or None,
                  observation_count=max(1, len(sources)), valid_from=valid_from,
                  last_confirmed_at=confirmed, invalid_at=invalid_at)
    db.add(f)
    db.commit()
    return f


def _at(s: str) -> datetime:
    return datetime.fromisoformat(s)


class TestKnownAsOf:
    def test_a_fact_restated_after_the_moment_asked_reads_as_it_was_then(
            self, db, tenant, client, head):
        first = _memory(db, tenant, NOW - timedelta(days=3))
        later = _memory(db, tenant, NOW - timedelta(hours=1), "Luca is still on the Pro plan.")
        f = _fact(db, tenant, learned=NOW - timedelta(days=3), sources=[first, later],
                  valid_from=NOW - timedelta(days=3), confirmed=NOW - timedelta(hours=1))
        asked = NOW - timedelta(days=1)

        (now,) = client.get("/v1/facts", headers=head, params={"user_id": "luca"}).json()["facts"]
        assert now["observation_count"] == 2
        assert set(now["source_memory_ids"]) == {mem_public_id(first.id), mem_public_id(later.id)}

        r = client.get("/v1/facts", headers=head,
                       params={"user_id": "luca", "known_as_of": asked.isoformat()})
        assert r.status_code == 200, r.text
        (then,) = r.json()["facts"]
        assert then["id"] == fact_public_id(f.id)
        assert then["source_memory_ids"] == [mem_public_id(first.id)], (
            "a memory written after the moment asked is a source of then")
        assert then["observation_count"] == 1
        assert _at(then["last_confirmed_at"]) <= asked, "a confirmation dated after the moment asked"
        assert _at(then["last_confirmed_at"]) == NOW - timedelta(days=3)

    def test_restatements_with_no_memory_keep_their_count(self, db, tenant, client, head):
        """A triple posted directly restates with no source, and no date to
        tell when: it is counted as it is now, and the confirmation still
        never reads after the moment asked."""
        f = _fact(db, tenant, learned=NOW - timedelta(days=3), sources=[],
                  valid_from=NOW - timedelta(days=3), confirmed=NOW - timedelta(hours=1))
        f.observation_count = 3
        db.commit()
        asked = NOW - timedelta(days=1)
        (then,) = client.get("/v1/facts", headers=head, params={
            "user_id": "luca", "known_as_of": asked.isoformat()}).json()["facts"]
        assert then["observation_count"] == 3 and then["source_memory_ids"] == []
        assert _at(then["last_confirmed_at"]) == asked


class TestTheHistoryIsDatedByTheStore:
    def test_a_memory_about_2020_learns_its_fact_when_it_is_written(self, db, tenant, client, head):
        m = _memory(db, tenant, NOW - timedelta(minutes=5), "In 2020 Luca moved to Turin.")
        _fact(db, tenant, learned=NOW - timedelta(minutes=4), sources=[m],
              valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc))
        ev = client.get(f"/v1/memories/{mem_public_id(m.id)}/history", headers=head).json()["events"]
        assert [e["event"] for e in ev] == ["created", "fact_extracted"]
        assert _at(ev[1]["at"]) == NOW - timedelta(minutes=4)

    def test_an_end_set_ahead_is_dated_when_it_was_learned(self, db, tenant, client, head):
        m = _memory(db, tenant, NOW - timedelta(minutes=5))
        f = _fact(db, tenant, learned=NOW - timedelta(minutes=4), sources=[m],
                  valid_from=NOW - timedelta(minutes=4),
                  invalid_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
        db.refresh(f)
        assert f.closed_known_at is not None, "the trigger writes when the end was learned"
        ev = client.get(f"/v1/memories/{mem_public_id(m.id)}/history", headers=head).json()["events"]
        (ended,) = [e for e in ev if e["event"] == "fact_invalidated"]
        assert _at(ended["at"]) == f.closed_known_at
        assert _at(ended["at"]) <= datetime.now(timezone.utc), "an event still to come"

    def test_a_fact_restated_is_learned_from_this_memory_after_it_was_written(
            self, db, tenant, client, head):
        first = _memory(db, tenant, NOW - timedelta(days=3))
        again = _memory(db, tenant, NOW - timedelta(minutes=5), "Luca is still on the Pro plan.")
        _fact(db, tenant, learned=NOW - timedelta(days=3), sources=[first, again],
              valid_from=NOW - timedelta(days=3))
        ev = client.get(f"/v1/memories/{mem_public_id(again.id)}/history",
                        headers=head).json()["events"]
        assert [e["event"] for e in ev] == ["created", "fact_extracted"]
        assert _at(ev[1]["at"]) >= _at(ev[0]["at"])


class _Judge:
    """A judge that finds no contradiction: the correction alone has to close
    the old value."""

    def complete(self, messages, **kw):
        prompt = messages[0]["content"]
        if is_the_judge(prompt):
            return LLMResult(text=in_the_judges_shape(prompt, '{"contradictions": []}'))
        if '"family"' in prompt:
            return LLMResult(text='{"family": "other"}')
        if "many_values" in prompt:
            return LLMResult(text='{"many_values": false}')
        return LLMResult(text='{"same_as": null}')


@pytest.mark.parametrize("judge", [False, True], ids=["no-model", "judge-sees-no-contradiction"])
def test_a_correction_of_a_fact_closed_ahead_closes_it_now(db, tenant, client, head, monkeypatch,
                                                          judge):
    if judge:
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: _Judge())
    r = client.post("/v1/facts", headers=head, json={
        "subject": "luca", "predicate": "on_plan", "object": "Pro plan", "user_id": "luca"})
    assert r.status_code == 201, r.text
    old = r.json()["id"]
    assert client.post(f"/v1/facts/{old}/forget", headers=head,
                       json={"at": "2030-01-01"}).status_code == 200

    c = client.patch(f"/v1/facts/{old}", headers=head, json={"object": "Business plan"})
    assert c.status_code == 200, c.text
    assert c.json()["invalidated"] == [old]

    now = client.get("/v1/facts", headers=head, params={"user_id": "luca"}).json()["facts"]
    assert [f["object"] for f in now] == ["Business plan"], "two values of one fact true now"
    db.expire_all()
    new = db.query(AgentFact).filter(AgentFact.object_label == "Business plan").one()
    was = db.query(AgentFact).filter(AgentFact.object_label == "Pro plan").one()
    assert was.invalid_at == new.valid_from, "the old value ends where the correction begins"
    assert was.invalidated_by_id == new.id
    assert was.invalid_at >= was.valid_from, "a fact never closes before it begins"


def test_a_correction_of_an_open_fact_is_unchanged(db, tenant, client, head):
    """Next to the fix, the case of before: an open fact, corrected, closes
    where the correction begins."""
    old = client.post("/v1/facts", headers=head, json={
        "subject": "luca", "predicate": "on_plan", "object": "Pro plan", "user_id": "luca"}).json()["id"]
    c = client.patch(f"/v1/facts/{old}", headers=head, json={"object": "Business plan"})
    assert c.status_code == 200, c.text
    assert c.json()["invalidated"] == [old]
    got = client.get("/v1/facts", headers=head, params={"user_id": "luca"}).json()["facts"]
    assert [f["object"] for f in got] == ["Business plan"]
    hist = client.get("/v1/facts", headers=head, params={
        "user_id": "luca", "include_invalidated": "true"}).json()["facts"]
    assert {f["object"] for f in hist} == {"Business plan", "Pro plan"}
