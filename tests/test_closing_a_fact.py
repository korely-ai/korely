"""Closing a fact over /v1: the half of the contract that was missing.

Facts could be read and written over the public API and only closed from the
dashboard, so an agent could assert what it had learned and never retract it.
Everything it got wrong stayed true forever unless a model happened to notice
the contradiction.

The commercial point is not the endpoint. It is that contradiction detection
stops being load bearing: an agent that knows a fact is finished says so, and
nothing has to infer it. That is the configuration with no model in the write
path at all, which is the one a buyer with a compliance question asks for.
"""
import uuid

import pytest

from korely_agent.services import keys as key_service



@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c


def _key(db, tenant, project_id=None, name=None):
    _row, plain = key_service.create_api_key(
        db, tenant_id=tenant.id, name=name or f"k-{uuid.uuid4().hex[:6]}",
        project_id=project_id, scopes=["memories:read", "memories:write"])
    return {"Authorization": f"Bearer {plain}"}


def _write(client, head, **kw):
    body = {"user_id": "u1", "subject": "u1", "predicate": "is_on",
            "object": "Gold plan", **kw}
    r = client.post("/v1/facts", headers=head, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


class TestAnAgentCanRetractWhatItSaid:

    def test_a_fact_can_be_closed_and_stays_in_history(self, db, tenant, client):
        head = _key(db, tenant)
        fid = _write(client, head)

        r = client.post(f"/v1/facts/{fid}/forget", headers=head)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "forgotten"
        assert r.json()["invalid_at"] is not None

        current = client.get("/v1/facts?user_id=u1", headers=head).json()["facts"]
        assert [f["id"] for f in current] == []
        with_history = client.get(
            "/v1/facts?user_id=u1&include_invalidated=true", headers=head).json()["facts"]
        assert fid in [f["id"] for f in with_history], "closing it deleted it"

    def test_closing_it_twice_changes_nothing_and_says_so(self, db, tenant, client):
        head = _key(db, tenant)
        fid = _write(client, head)
        client.post(f"/v1/facts/{fid}/forget", headers=head)
        again = client.post(f"/v1/facts/{fid}/forget", headers=head)
        assert again.status_code == 200
        assert again.json()["status"] == "already_forgotten"

    def test_it_closes_on_the_date_the_world_changed_not_today(self, db, tenant, client):
        """The whole reason for keeping history rather than deleting rows."""
        head = _key(db, tenant)
        fid = _write(client, head, valid_from="2026-01-10")

        r = client.post(f"/v1/facts/{fid}/forget", headers=head,
                        json={"at": "2026-06-20"})
        assert r.status_code == 200, r.text
        assert r.json()["invalid_at"].startswith("2026-06-20")

        # Before it ended: still true. After: gone.
        march = client.get("/v1/facts?user_id=u1&as_of=2026-03-01", headers=head)
        assert fid in [f["id"] for f in march.json()["facts"]]
        august = client.get("/v1/facts?user_id=u1&as_of=2026-08-01", headers=head)
        assert fid not in [f["id"] for f in august.json()["facts"]]

    def test_an_end_still_ahead_can_be_brought_earlier(self, db, tenant, client):
        """A contract to end in 2030, cancelled in June (audit of 17 Oct 2026):
        the second forget answered already_forgotten and left 2030, so every
        read said the fact was still true."""
        head = _key(db, tenant)
        fid = _write(client, head, valid_from="2026-01-01")
        first = client.post(f"/v1/facts/{fid}/forget", headers=head, json={"at": "2030-01-01"})
        assert first.json()["status"] == "forgotten"

        r = client.post(f"/v1/facts/{fid}/forget", headers=head, json={"at": "2026-06-01"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "forgotten"
        assert r.json()["invalid_at"].startswith("2026-06-01")
        current = client.get("/v1/facts?user_id=u1", headers=head).json()["facts"]
        assert fid not in [f["id"] for f in current]
        may = client.get("/v1/facts?user_id=u1&as_of=2026-05-01", headers=head).json()["facts"]
        assert fid in [f["id"] for f in may]

    def test_an_end_still_ahead_is_closed_now_by_a_bare_forget(self, db, tenant, client):
        head = _key(db, tenant)
        fid = _write(client, head, valid_from="2026-01-01")
        client.post(f"/v1/facts/{fid}/forget", headers=head, json={"at": "2030-01-01"})
        r = client.post(f"/v1/facts/{fid}/forget", headers=head)
        assert r.json()["status"] == "forgotten"
        assert not r.json()["invalid_at"].startswith("2030")
        assert client.get("/v1/facts?user_id=u1", headers=head).json()["facts"] == []

    def test_a_later_end_is_not_a_forget(self, db, tenant, client):
        """Moving an end later would make the fact true again on days the store
        had it over: that is a correction, not a forget."""
        head = _key(db, tenant)
        fid = _write(client, head, valid_from="2026-01-01")
        client.post(f"/v1/facts/{fid}/forget", headers=head, json={"at": "2030-01-01"})
        r = client.post(f"/v1/facts/{fid}/forget", headers=head, json={"at": "2031-01-01"})
        assert r.json()["status"] == "already_forgotten"
        assert r.json()["invalid_at"].startswith("2030-01-01")

    def test_a_fact_can_be_corrected_and_both_stay_readable(self, db, tenant, client):
        head = _key(db, tenant)
        fid = _write(client, head)

        r = client.patch(f"/v1/facts/{fid}", headers=head,
                         json={"object": "Bronze plan"})
        assert r.status_code == 200, r.text
        new_id = r.json()["id"]
        assert new_id != fid

        current = client.get("/v1/facts?user_id=u1", headers=head).json()["facts"]
        assert [f["object"] for f in current] == ["Bronze plan"]
        assert new_id in [f["id"] for f in current], (
            "the correction is unreachable through the API, which is the defect of "
            "14 Sep 2026 coming back on a new road")

    def test_a_correction_with_nothing_in_it_is_refused(self, db, tenant, client):
        head = _key(db, tenant)
        fid = _write(client, head)
        r = client.patch(f"/v1/facts/{fid}", headers=head, json={})
        assert r.status_code == 422


class TestTheWallHoldsOnTheNewRoads:
    """Two new ways to reach a fact is two new ways to reach somebody else's."""

    def _two(self, db, tenant, client):
        from korely_agent.db.models import AgentProject

        a = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="Alpha",
                         slug="alpha", is_default=False)
        b = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="Beta",
                         slug="beta", is_default=False)
        db.add_all([a, b]); db.commit()
        ka = _key(db, tenant, project_id=a.id, name="alpha-key")
        kb = _key(db, tenant, project_id=b.id, name="beta-key")
        return ka, kb, _write(client, ka)

    def test_beta_cannot_close_alphas_fact(self, db, tenant, client):
        ka, kb, fid = self._two(db, tenant, client)
        r = client.post(f"/v1/facts/{fid}/forget", headers=kb)
        assert r.status_code == 404, (
            "a key confined to Beta closed a fact in Alpha")
        still = client.get("/v1/facts?user_id=u1", headers=ka).json()["facts"]
        assert fid in [f["id"] for f in still], "and it actually closed it"

    def test_beta_cannot_correct_alphas_fact(self, db, tenant, client):
        ka, kb, fid = self._two(db, tenant, client)
        r = client.patch(f"/v1/facts/{fid}", headers=kb, json={"object": "x"})
        assert r.status_code == 404

    def test_a_made_up_id_answers_the_same_as_somebody_elses(self, db, tenant, client):
        """Otherwise the difference between the two answers tells a prober
        which ids are real."""
        ka, kb, fid = self._two(db, tenant, client)
        theirs = client.post(f"/v1/facts/{fid}/forget", headers=kb)
        invented = client.post("/v1/facts/fct_" + "0" * 32 + "/forget", headers=kb)
        assert theirs.status_code == invented.status_code == 404


class TestTheAuditCallsItWhatItIs:

    def test_closing_a_fact_is_recorded_as_an_invalidation(self, db, tenant, client):
        """Neither a read nor an erasure: the fact stops being said and its
        row stays in history. `erase` until 1.0.23 (review of 1 Nov 2026)."""
        from korely_agent.db.models import AuditEvent

        head = _key(db, tenant)
        fid = _write(client, head)
        client.post(f"/v1/facts/{fid}/forget", headers=head)

        rows = db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id).all()
        actions = {r.action for r in rows
                   if (r.meta or {}).get("endpoint") == "facts:forget"}
        assert actions == {"fact_invalidate"}, (
            f"recorded as {actions}; answering a data subject's access request "
            f"with the wrong verb is answering it wrongly")


class TestAFactCannotStopBeforeItStarted:
    """`at` earlier than the fact's own valid_from makes a row true over an
    empty interval: not history, but a fact no point-in-time read can ever
    return, at any date.

    Found by driving the endpoint from an n8n node and watching a fact close in
    June that had started in September. The API said 200 and the row was
    nonsense. Compiling and answering 200 are not the same as working.
    """

    def test_it_is_refused_and_the_message_carries_both_dates(self, db, tenant, client):
        head = _key(db, tenant)
        fid = _write(client, head, valid_from="2026-09-01")

        r = client.post(f"/v1/facts/{fid}/forget", headers=head,
                        json={"at": "2026-06-20"})
        assert r.status_code == 422, r.text
        message = r.json()["detail"]["message"]
        assert "2026-09-01" in message and "2026-06-20" in message, message

    def test_the_fact_is_left_open(self, db, tenant, client):
        """A refused close must not half-close it."""
        head = _key(db, tenant)
        fid = _write(client, head, valid_from="2026-09-01")
        client.post(f"/v1/facts/{fid}/forget", headers=head, json={"at": "2026-06-20"})

        still = client.get("/v1/facts?user_id=u1", headers=head).json()["facts"]
        assert fid in [f["id"] for f in still]

    def test_closing_at_the_moment_it_began_retracts_it(self, db, tenant, client):
        """`at` equal to valid_from leaves [t, t): true at no moment, kept in
        the history. Not "true for an instant", as this test said until the
        audit of 17 Oct 2026: a retraction, the same row the store writes
        for a fact dated ahead that is forgotten. Only going backwards is
        refused."""
        head = _key(db, tenant)
        fid = _write(client, head, valid_from="2026-09-01")
        r = client.post(f"/v1/facts/{fid}/forget", headers=head,
                        json={"at": "2026-09-01"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "forgotten"
        for when in ("2026-09-01T00:00:00Z", "2026-09-01T00:00:00.000001Z",
                     "2026-09-01T12:00:00Z"):
            seen = client.get(f"/v1/facts?user_id=u1&as_of={when}", headers=head).json()["facts"]
            assert fid not in [f["id"] for f in seen], when
        history = client.get("/v1/facts?user_id=u1&include_invalidated=true",
                             headers=head).json()["facts"]
        (row,) = [f for f in history if f["id"] == fid]
        assert row["invalid_at"] == row["valid_from"]

    def test_a_refusal_on_the_same_day_says_the_instants(self, db, tenant, client):
        """Begun at noon, closed at eight that morning: the message said
        "began on 2026-09-01 ... on 2026-09-01, which is earlier"."""
        head = _key(db, tenant)
        fid = _write(client, head, valid_from="2026-09-01T12:00:00Z")
        r = client.post(f"/v1/facts/{fid}/forget", headers=head,
                        json={"at": "2026-09-01T08:00:00Z"})
        assert r.status_code == 422, r.text
        message = r.json()["detail"]["message"]
        assert "12:00" in message and "08:00" in message, message

    def test_a_bare_forget_of_a_fact_dated_ahead_ends_at_its_start(self, db, tenant, client):
        """No `at` means now, and now is before a fact dated ahead began: it
        was closed now, an end before its start (audit of 17 Oct 2026). It
        ends where it would have begun, as a forgotten memory's facts do."""
        from datetime import datetime, timedelta, timezone

        head = _key(db, tenant)
        ahead = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0)
        fid = _write(client, head, valid_from=ahead.isoformat())
        r = client.post(f"/v1/facts/{fid}/forget", headers=head)
        assert r.status_code == 200, r.text
        history = client.get("/v1/facts?user_id=u1&include_invalidated=true",
                             headers=head).json()["facts"]
        (row,) = [f for f in history if f["id"] == fid]
        assert row["invalid_at"] == row["valid_from"], row

    def test_and_a_later_date_still_works(self, db, tenant, client):
        head = _key(db, tenant)
        fid = _write(client, head, valid_from="2026-01-10")
        r = client.post(f"/v1/facts/{fid}/forget", headers=head,
                        json={"at": "2026-06-20"})
        assert r.status_code == 200, r.text


class TestNowIsAMomentLikeAnyOther:
    """A fact closed on a future date is still true today.

    A tester closed one in the year 3026 and got two answers to the same
    question on the same day: the default read said it was gone, `as_of` today
    said it was there. `as_of` asked the right question — began before then and
    had not ended by then — while every read that meant "now" asked a narrower
    one: has it no end date at all.

    The two agreed for as long as an end date could only be in the past, and
    stopped agreeing the day `forget` learned to take a date. The repair opened
    the door and the rest of the product had not been told.

    Closing a fact on a future date is the obvious use of that parameter: a
    contract that ends on 31 December, a subscription with a known last day.
    """

    def _closed_in(self, client, head, when, obj="Priority Support plan"):
        fid = _write(client, head, object=obj, valid_from="2026-03-15")
        r = client.post(f"/v1/facts/{fid}/forget", headers=head, json={"at": when})
        assert r.status_code == 200, r.text
        return fid

    def test_the_default_read_and_as_of_today_agree(self, db, tenant, client):
        head = _key(db, tenant)
        fid = self._closed_in(client, head, "3026-01-01")

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()

        now_facts = client.get("/v1/facts?user_id=u1", headers=head).json()["facts"]
        today_facts = client.get(f"/v1/facts?user_id=u1&as_of={today}", headers=head).json()["facts"]
        assert [f["id"] for f in now_facts] == [f["id"] for f in today_facts], (
            "the same question on the same day gives two answers")
        assert fid in [f["id"] for f in now_facts]

    def test_a_fact_closed_in_the_past_is_gone(self, db, tenant, client):
        """The mirror, so the test measures the date and not the branch."""
        head = _key(db, tenant)
        fid = self._closed_in(client, head, "2026-06-01")
        now_facts = client.get("/v1/facts?user_id=u1", headers=head).json()["facts"]
        assert fid not in [f["id"] for f in now_facts]

    def test_the_profile_agrees_too(self, db, tenant, client):
        """Where an agent actually reads it."""
        head = _key(db, tenant)
        fid = self._closed_in(client, head, "3026-01-01")
        profile = client.get("/v1/profile?user_id=u1", headers=head).json()
        assert fid in [f["id"] for f in profile["facts"]]

    def test_and_a_fact_not_yet_begun_is_not_true_yet(self, db, tenant, client):
        """The same predicate, from the other end: `valid_from` in the future
        means not true yet, and the read that means "now" has to say so."""
        head = _key(db, tenant)
        fid = _write(client, head, object="Future plan", valid_from="3026-01-01")
        now_facts = client.get("/v1/facts?user_id=u1", headers=head).json()["facts"]
        assert fid not in [f["id"] for f in now_facts], (
            "a fact that starts in a thousand years is being served as true now")

    def test_writing_the_same_triple_again_reuses_the_future_closed_one(
            self, db, tenant, client):
        """It is currently true, so a second write must not make a second one."""
        head = _key(db, tenant)
        fid = self._closed_in(client, head, "3026-01-01")
        again = client.post("/v1/facts", headers=head, json={
            "user_id": "u1", "subject": "u1", "predicate": "is_on",
            "object": "Priority Support plan"})
        assert again.status_code == 201
        assert again.json()["id"] == fid, "two identical facts are true at once"


class TestTheErrorNamesTheFieldYouSent:
    """One parser serves `as_of` and `at`, and the message was hard-coded to
    the first. Somebody who sent `at` was told to fix `as_of`, which is not in
    their request."""

    def test_a_bad_at_says_at(self, db, tenant, client):
        head = _key(db, tenant)
        fid = _write(client, head)
        r = client.post(f"/v1/facts/{fid}/forget", headers=head,
                        json={"at": "not-a-date"})
        assert r.status_code == 422
        assert "at must be" in r.text, r.text
        assert "as_of" not in r.text

    def test_a_bad_as_of_still_says_as_of(self, db, tenant, client):
        head = _key(db, tenant)
        r = client.get("/v1/facts?user_id=u1&as_of=not-a-date", headers=head)
        assert r.status_code == 422
        assert "as_of must be" in r.text
