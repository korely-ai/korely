"""The delivery queue of the webhooks, as the hosted product's (2026-10-06):
the schedule of the Standard Webhooks specification, an endpoint switched off
only after about three days of nothing, Enable, a ceiling of endpoints, the
accounts taking turns, and a whole attempt within 15 s. No receiver is
contacted: `_deliver` is replaced.
"""
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.thread_helpers import held_now, wait_idle

from korely_agent.db.models import AgentWebhookDelivery, AgentWebhookEndpoint, Tenant
from korely_agent.services import keys as key_service
from korely_agent.services import webhooks



# A Standard Webhooks secret (0.2.0 sends no endpoint with a secret of before 0.1.11).
SECRET = "whsec_AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="  # bytes 0..31, gitleaks:allow

def _endpoint(db, tenant, **kw):
    ep = AgentWebhookEndpoint(id=uuid.uuid4(), tenant_id=tenant.id, url="https://hooks.example/x",
                              secret=SECRET, events=["memory.created"], active=True, **kw)
    db.add(ep)
    db.commit()
    return ep


def _deliveries(db, tenant, n):
    for i in range(n):
        webhooks.emit_event(db, tenant_id=tenant.id, event_type="memory.created",
                            data={"id": f"mem_{i}"})
    return db.query(AgentWebhookDelivery).filter(AgentWebhookDelivery.tenant_id == tenant.id).all()


def _wait(d):
    when = d.next_retry_at if d.next_retry_at.tzinfo else d.next_retry_at.replace(tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).total_seconds()


class TestTheSchedule:
    def test_ten_attempts_over_three_days(self):
        assert webhooks.ATTEMPTS == 10
        assert sum(webhooks.SCHEDULE) == 75 * 3600 + 35 * 60 + 5

    def test_each_wait_follows_the_schedule_within_ten_percent(self, db, tenant, monkeypatch):
        monkeypatch.setattr(webhooks, "_deliver", lambda *a, **k: (False, 503, "HTTP 503"))
        _endpoint(db, tenant)
        (d,) = _deliveries(db, tenant, 1)
        assert d.max_attempts == 10
        webhooks.process_deliveries(db)
        db.refresh(d)
        assert 4.0 <= _wait(d) <= 5.6, "after the first attempt: 5 s"
        d.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        webhooks.process_deliveries(db)
        db.refresh(d)
        assert 265 <= _wait(d) <= 331, "after the second: 5 min"


class TestSwitchedOffOnlyAfterDaysOfNothing:
    def test_an_endpoint_that_accepted_one_since_stays_on(self, db, tenant, monkeypatch):
        monkeypatch.setattr(webhooks, "_deliver", lambda *a, **k: (False, 503, "HTTP 503"))
        ep = _endpoint(db, tenant)
        (d,) = _deliveries(db, tenant, 1)
        d.attempts = webhooks.ATTEMPTS - 1
        ep.last_delivery_at = datetime.now(timezone.utc)     # one went through since
        db.commit()
        webhooks.process_deliveries(db)
        db.refresh(ep)
        db.refresh(d)
        assert d.next_retry_at is None and ep.active is True


class TestTheAccountsTakeTurns:
    def test_a_backlog_of_one_does_not_hold_the_others(self, db, tenant, monkeypatch):
        monkeypatch.setattr(webhooks, "_deliver", lambda *a, **k: (True, 204, None))
        other = Tenant(id=uuid.uuid4(), name="Somebody Else")
        db.add(other)
        db.commit()
        _endpoint(db, tenant)
        _endpoint(db, other)
        _deliveries(db, tenant, 6)          # the older backlog
        _deliveries(db, other, 1)
        out = webhooks.process_deliveries(db, limit=3)
        sent = db.query(AgentWebhookDelivery).filter(
            AgentWebhookDelivery.status == webhooks.STATUS_DELIVERED).all()
        assert out["delivered"] == 3
        assert sorted(str(x.tenant_id) for x in sent).count(str(other.id)) == 1

    def test_an_account_alone_takes_the_whole_round(self, db, tenant, monkeypatch):
        """Two a round, whatever the limit, until the audit of 18 Oct 2026:
        about 480 an hour, and a backlog that only grew. A full
        round says `more`, and the worker goes on at once."""
        monkeypatch.setattr(webhooks, "_deliver", lambda *a, **k: (True, 204, None))
        _endpoint(db, tenant)
        _deliveries(db, tenant, 20)
        db.commit()
        assert webhooks.process_deliveries(db, limit=20) == {"delivered": 20, "failed": 0,
                                                             "more": True}
        assert webhooks.process_deliveries(db, limit=20) == {"delivered": 0, "failed": 0}

    def test_older_backlogs_do_not_hold_a_younger_account(self, db, tenant, monkeypatch):
        """Ten accounts with an older backlog took every place of a round of
        twenty, oldest first across the install (audit of 18 Oct
        2026): each account's turn comes before anybody's age.

        The claim decides it: the young delivery is in the round's first turn
        (eleven accounts, twenty places). Its attempt then writes its outcome
        in a thread of its own (core/threads.Lanes), and a round waits for
        its attempts `_ROUND_PATIENCE` (1 s) and no longer: on a loaded
        machine the attempt, started in the round, had not written yet when
        the round returned, or had not started (eight threads, one each
        account), and the test read `pending` (once in a full
        suite). So the round here waits for every attempt it starts, after
        the attempts of earlier tests have ended; the patience itself is
        TestAnAttemptHasFifteenSeconds' subject, not this one's."""
        assert wait_idle(webhooks._sending, 30), "an earlier test's attempts still hold the threads"
        monkeypatch.setattr(webhooks, "_ROUND_PATIENCE", 60.0)
        monkeypatch.setattr(webhooks, "_deliver", lambda *a, **k: (True, 204, None))
        long_ago = datetime.now(timezone.utc) - timedelta(hours=3)
        for i in range(10):
            busy = Tenant(id=uuid.uuid4(), name=f"Busy {i}")
            db.add(busy)
            db.commit()
            _endpoint(db, busy)
            for d in _deliveries(db, busy, 4):
                d.created_at = long_ago
        _endpoint(db, tenant)
        (young,) = _deliveries(db, tenant, 1)
        db.commit()
        webhooks.process_deliveries(db, limit=20)
        db.refresh(young)
        assert young.attempts == 1, "the young delivery was not in the round"
        assert young.status == webhooks.STATUS_DELIVERED

    def test_more_accounts_than_places_take_turns(self, db, tenant, monkeypatch):
        """Five accounts, two places a round: each is served within
        ceil(5 / 2) = 3 rounds, however old the others' backlogs."""
        monkeypatch.setattr(webhooks, "_deliver", lambda *a, **k: (True, 204, None))
        monkeypatch.setattr(webhooks, "_served_last", None)
        accounts = [tenant]
        for i in range(4):
            accounts.append(Tenant(id=uuid.uuid4(), name=f"Account {i}"))
            db.add(accounts[-1])
            db.commit()
        for i, t in enumerate(accounts):
            _endpoint(db, t)
            for d in _deliveries(db, t, 3):
                # The first accounts the oldest: by age alone they would
                # take every round.
                d.created_at = datetime.now(timezone.utc) - timedelta(hours=10 - i)
        db.commit()
        for _ in range(3):
            assert webhooks.process_deliveries(db, limit=2)["more"] is True
        served = {r.tenant_id for r in db.query(AgentWebhookDelivery).filter(
            AgentWebhookDelivery.status == webhooks.STATUS_DELIVERED)}
        assert served == {t.id for t in accounts}


class TestAnAttemptHasFifteenSeconds:
    def test_a_receiver_that_does_not_finish_is_a_failure(self, db, tenant, monkeypatch):
        monkeypatch.setattr(webhooks, "_ATTEMPT_SECONDS", 0.3)

        def slow(*a, **k):
            time.sleep(2)
            return True, 204, None

        monkeypatch.setattr(webhooks, "_deliver", slow)
        _endpoint(db, tenant)
        (d,) = _deliveries(db, tenant, 1)
        started = time.monotonic()
        webhooks.process_deliveries(db)
        assert time.monotonic() - started < 1.5, "the round waited for the slow receiver"
        # The attempt writes its own outcome when it ends (core/threads.Lanes).
        assert wait_idle(webhooks._sending, 5)
        db.refresh(d)
        assert d.status == webhooks.STATUS_FAILED and "within" in d.last_error


class TestARoundDoesNotWaitForTheSlowestAccount:
    """Audit of 20 Oct 2026: a round waited for its slowest attempt, so an
    account whose receiver held each attempt to its deadline set the pace of
    every account's next round, and its attempts took all eight threads while
    the others' queued."""

    def test_a_stalling_account_holds_its_share_and_the_others_go_out(self, db, tenant,
                                                                      monkeypatch):
        import threading as _threading

        monkeypatch.setattr(webhooks, "_ATTEMPT_SECONDS", 1.0)
        lock = _threading.Lock()
        flight = {"now": 0, "most": 0, "started": 0}

        def deliver(url, secret, delivery_id, payload):
            if "stalls" not in url:
                return True, 204, None
            with lock:
                flight["now"] += 1
                flight["started"] += 1
                flight["most"] = max(flight["most"], flight["now"])
            try:
                time.sleep(1.0)       # held to its deadline
                return False, None, "no complete answer"
            finally:
                with lock:
                    flight["now"] -= 1

        monkeypatch.setattr(webhooks, "_deliver", deliver)
        stalling = Tenant(id=uuid.uuid4(), name="Stalling")
        db.add(stalling)
        db.commit()
        slow = AgentWebhookEndpoint(id=uuid.uuid4(), tenant_id=stalling.id,
                                    url="https://stalls.example/x", secret=SECRET,
                                    events=["memory.created"], active=True)
        db.add(slow)
        db.commit()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        for d in _deliveries(db, stalling, 10):
            d.created_at = long_ago          # the older backlog
        _endpoint(db, tenant)
        fast = _deliveries(db, tenant, 10)
        db.commit()
        started = time.monotonic()
        webhooks.process_deliveries(db, limit=20)
        took = time.monotonic() - started
        assert took < 1.6, f"the round waited {took:.1f} s for the stalling account"
        for d in fast:
            db.refresh(d)
        assert all(d.status == webhooks.STATUS_DELIVERED for d in fast)
        assert flight["most"] <= webhooks._IN_FLIGHT // 2, flight
        assert wait_idle(webhooks._sending, 10)
        # The stalled attempts wrote their own outcomes; the ones never
        # started went back to the queue with their attempt given back.
        rows = db.query(AgentWebhookDelivery).filter(
            AgentWebhookDelivery.tenant_id == stalling.id).all()
        for r in rows:
            db.refresh(r)
        assert sum(r.status == webhooks.STATUS_FAILED for r in rows) == flight["started"]
        assert all(r.attempts == 0 for r in rows if r.status == webhooks.STATUS_PENDING)


class TestALateRoundChangesNothing:
    """A round writes its outcomes over its own claims only (audit of 17 Oct
    2026): its lease ran out, another drain claimed the delivery again, and
    the first round's late answer used to overwrite the second's."""

    def test_a_delivery_claimed_again_meanwhile_keeps_the_new_claim(
            self, db, engine, tenant, monkeypatch):
        from sqlalchemy.orm import sessionmaker

        ep = _endpoint(db, tenant)
        (d,) = _deliveries(db, tenant, 1)
        other = sessionmaker(bind=engine, expire_on_commit=False)

        def deliver_while_another_drain_claims_it(url, secret, delivery_id, payload):
            # The other drain, its lease run out here: one more attempt, a
            # lease of its own.
            with other() as s:
                row = s.get(AgentWebhookDelivery, uuid.UUID(delivery_id))
                row.attempts += 1
                row.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=300)
                s.commit()
            return True, 204, None

        monkeypatch.setattr(webhooks, "_deliver", deliver_while_another_drain_claims_it)
        out = webhooks.process_deliveries(db)
        assert out == {"delivered": 0, "failed": 0}
        with other() as s:
            row = s.get(AgentWebhookDelivery, d.id)
            again = s.get(AgentWebhookEndpoint, ep.id)
            assert (row.status, row.attempts, row.delivered_at) == (webhooks.STATUS_PENDING, 2, None)
            assert _wait(row) > 200
            assert again.last_delivery_at is None

    def test_the_lease_covers_the_round(self, db, engine, tenant, monkeypatch):
        """Two attempts one after the other, each with its 100 s: the claim
        holds past both, so no drain takes them while this one is on them."""
        from sqlalchemy.orm import sessionmaker

        monkeypatch.setattr(webhooks, "_IN_FLIGHT", 1)
        monkeypatch.setattr(webhooks, "_ATTEMPT_SECONDS", 100.0)
        _endpoint(db, tenant)
        _deliveries(db, tenant, 2)
        other = sessionmaker(bind=engine, expire_on_commit=False)
        leases = []

        def deliver(url, secret, delivery_id, payload):
            with other() as s:
                leases.append(_wait(s.get(AgentWebhookDelivery, uuid.UUID(delivery_id))))
            return True, 204, None

        monkeypatch.setattr(webhooks, "_deliver", deliver)
        assert webhooks.process_deliveries(db)["delivered"] == 2
        assert min(leases) > 200, leases


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setattr("korely_agent.services.webhooks.check_url", lambda u: u.strip())
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin(db, tenant):
    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="admin",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    return {"Authorization": f"Bearer {full}"}


class TestTheScreen:
    def test_a_ceiling_of_endpoints(self, client, admin):
        for i in range(webhooks.MAX_ENDPOINTS):
            assert client.post("/admin/webhooks", headers=admin,
                               json={"url": f"https://hooks.example/{i}"}).status_code == 201
        r = client.post("/admin/webhooks", headers=admin, json={"url": "https://hooks.example/x"})
        assert r.status_code == 409 and r.json()["code"] == "too_many_endpoints"

    def test_enable_keeps_the_secret_and_forgets_the_failures(self, db, tenant, client, admin):
        ep = _endpoint(db, tenant, failure_count=12)
        ep.active = False
        db.commit()
        assert client.post(f"/admin/webhooks/{ep.id}/test", headers=admin).status_code == 409
        r = client.post(f"/admin/webhooks/{ep.id}/enable", headers=admin)
        assert r.status_code == 200 and r.json()["active"] is True
        db.refresh(ep)
        assert (ep.active, ep.failure_count, ep.secret) == (True, 0, SECRET)

    def test_enable_checks_the_address_again(self, db, tenant, client, admin, monkeypatch):
        def refuse(u):
            raise webhooks.UnsafeWebhookUrl("hooks.example resolves to 10.0.0.1, not public")

        monkeypatch.setattr("korely_agent.services.webhooks.check_url", refuse)
        ep = _endpoint(db, tenant)
        ep.active = False
        db.commit()
        assert client.post(f"/admin/webhooks/{ep.id}/enable", headers=admin).status_code == 400
        db.refresh(ep)
        assert ep.active is False

    def test_enable_looks_the_name_up_with_no_transaction_open(self, db, tenant, client, admin,
                                                                monkeypatch):
        """The lookup held a connection of the pool (audit of 18 Oct 2026)."""
        seen = []
        monkeypatch.setattr("korely_agent.services.webhooks.check_url",
                            lambda u: seen.append(db.in_transaction()) or u)
        ep = _endpoint(db, tenant)
        ep.active = False
        db.commit()
        assert client.post(f"/admin/webhooks/{ep.id}/enable", headers=admin).status_code == 200
        assert seen == [False]
        db.refresh(ep)
        assert ep.active is True

    @pytest.mark.parametrize("for_a_project", [False, True])
    def test_create_looks_the_name_up_with_no_transaction_open(self, db, tenant, client, admin,
                                                                monkeypatch, for_a_project):
        """The create kept the connection its key and project reads took
        (audit of 19 Oct 2026)."""
        from tests.conftest import seeded_project_id

        seen = []
        monkeypatch.setattr("korely_agent.services.webhooks.check_url",
                            lambda u: seen.append(db.in_transaction()) or u)
        body = {"url": "https://hooks.example/in", "events": ["fact.invalidated"]}
        if for_a_project:
            body["project_id"] = str(seeded_project_id(db, tenant.id))
            db.commit()
        r = client.post("/admin/webhooks", headers=admin, json=body)
        assert r.status_code == 201, r.text
        assert seen == [False]


class TestAnEndpointOfBefore0111SaysSo:
    """An endpoint with a secret of before 0.1.11 is not sent since 0.2.0. It
    was skipped at the enqueue with no row (audit of 19 Oct 2026), so the page
    showed no failure and no error while every event was lost, and its note
    said the receiver kept working. Its deliveries are now queued and
    refused at each attempt, as `_deliver` refused a queued one."""

    OLD = "whsec_" + "A" * 32  # the shape of `token_urlsafe(24)`

    def test_its_events_are_refused_where_the_page_shows_them(self, db, tenant, client, admin):
        ep = _endpoint(db, tenant)
        ep.secret = self.OLD
        db.commit()
        (d,) = _deliveries(db, tenant, 1)
        assert webhooks.process_deliveries(db) == {"delivered": 0, "failed": 1}
        db.refresh(d)
        assert d.status == webhooks.STATUS_FAILED and "rotate-webhook-secret" in d.last_error
        # The attempt wrote its outcome in a session of its own (`_send_one`):
        # this one reads the endpoint again.
        db.expire_all()
        r = client.get("/admin/webhooks", headers=admin)
        (shown,) = [h for h in r.json() if h["id"] == str(ep.id)]
        assert shown["legacy_signature"] is True
        assert shown["failure_count"] == 1 and shown["last_error"] == webhooks.LEGACY_REFUSAL

    def test_a_rotation_within_the_attempts_sends_what_is_queued(self, db, tenant, monkeypatch):
        ep = _endpoint(db, tenant)
        ep.secret = self.OLD
        db.commit()
        (d,) = _deliveries(db, tenant, 1)
        webhooks.process_deliveries(db)
        webhooks.rotate_secret(db, tenant.id, ep.id)
        sent = []
        monkeypatch.setattr(webhooks, "_deliver",
                            lambda url, secret, *a: sent.append(secret) or (True, 204, None))
        db.refresh(d)
        d.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        assert webhooks.process_deliveries(db) == {"delivered": 1, "failed": 0}
        db.refresh(ep)
        assert sent == [ep.secret] and not webhooks.is_legacy_secret(ep.secret)


class TestTheTestHoldsNothing:
    """Send a test kept its connection of the pool, and a thread, for the
    whole attempt: fifteen tests of one account at once held the pool, and
    every other account's requests answered 503 (audit of 18 Oct 2026)."""

    def test_no_transaction_is_open_while_the_receiver_is_called(self, db, tenant, client,
                                                                 admin, engine, monkeypatch):
        seen = []

        def deliver(url, secret, delivery_id, payload):
            seen.append(db.in_transaction())
            return True, 204, None

        monkeypatch.setattr(webhooks, "_deliver", deliver)
        ep = _endpoint(db, tenant)
        r = client.post(f"/admin/webhooks/{ep.id}/test", headers=admin)
        assert r.status_code == 200 and r.json()["delivered"] is True, r.text
        assert seen == [False]

    def test_an_account_runs_two_tests_at_once_at_most(self, db, tenant, client, admin,
                                                      monkeypatch):
        monkeypatch.setattr(webhooks, "_deliver", lambda *a, **k: (True, 204, None))
        ep = _endpoint(db, tenant)
        with webhooks._an_outbound_place(tenant.id), webhooks._an_outbound_place(tenant.id):
            r = client.post(f"/admin/webhooks/{ep.id}/test", headers=admin)
            assert r.status_code == 429 and r.json()["code"] == "rate_limit_exceeded", r.text
            assert r.headers["Retry-After"] == "15"
            other = Tenant(id=uuid.uuid4(), name="Somebody Else")
            db.add(other)
            db.commit()
            theirs = _endpoint(db, other)
            assert webhooks.send_test(db, other.id, theirs.id)["delivered"] is True, \
                "another account's test is not held"
        assert client.post(f"/admin/webhooks/{ep.id}/test", headers=admin).status_code == 200
        assert held_now(webhooks._outbound) == {}


class TestALookupHoldsNoRequestThreadForLong:
    """Audit of 20 Oct 2026: a create and an enable looked the webhook's host
    up on the request's thread, with no deadline and no per-account bound,
    unlike Send a test: one account's slow names held every request thread
    of the process. Now they take one of the account's outbound places
    (core/threads.OUTBOUND_PER_ACCOUNT, shared with the tests), and the lookup
    has core/threads.LOOKUP_SECONDS."""

    def test_past_its_places_a_create_and_an_enable_are_429(self, db, tenant, client, admin):
        ep = _endpoint(db, tenant)
        with webhooks._an_outbound_place(tenant.id), webhooks._an_outbound_place(tenant.id):
            r = client.post("/admin/webhooks", headers=admin,
                            json={"url": "https://hooks.example/more"})
            assert r.status_code == 429 and r.json()["code"] == "rate_limit_exceeded", r.text
            assert r.headers["Retry-After"] == "15"
            r = client.post(f"/admin/webhooks/{ep.id}/enable", headers=admin)
            assert r.status_code == 429, r.text
            other = Tenant(id=uuid.uuid4(), name="Somebody Else")
            db.add(other)
            db.commit()
            assert webhooks.create_endpoint(db, other.id, url="https://hooks.example/theirs",
                                            events=None) is not None, \
                "another account's create is not held"
        assert client.post("/admin/webhooks", headers=admin,
                           json={"url": "https://hooks.example/more"}).status_code == 201
        assert held_now(webhooks._outbound) == {}

    def test_a_slow_name_is_given_up_and_still_holds_the_place(self, db, tenant, monkeypatch):
        import socket
        import threading as _threading

        from korely_agent.core import threads

        monkeypatch.setattr(threads, "LOOKUP_SECONDS", 0.3)
        release = _threading.Event()

        def slow(host, *a, **k):
            release.wait(10)
            raise socket.gaierror(socket.EAI_AGAIN, "no answer")

        monkeypatch.setattr(socket, "getaddrinfo", slow)
        started = time.monotonic()
        with pytest.raises(webhooks.UnsafeWebhookUrl) as e:
            webhooks.create_endpoint(db, tenant.id, url="https://h1.slow.invalid/x", events=None)
        assert time.monotonic() - started < 2.0
        assert "did not resolve within" in str(e.value)
        # The caller is free, the lookup is not: it keeps the account's place.
        assert held_now(webhooks._outbound) == {str(tenant.id): 1}
        release.set()
        for _ in range(100):
            if not held_now(webhooks._outbound):
                break
            time.sleep(0.05)
        assert held_now(webhooks._outbound) == {}


class TestTheCeilingOfEndpointsHoldsUnderConcurrency:
    """Audit of 20 Oct 2026: the count
    and the insert were unlocked, and creates whose lookups answered together
    took an account a few endpoints past MAX_ENDPOINTS."""

    def test_creates_that_count_together_stop_at_the_ceiling(self, db, engine, tenant,
                                                              monkeypatch):
        import threading as _threading

        from sqlalchemy.orm import sessionmaker

        for i in range(webhooks.MAX_ENDPOINTS - 1):
            _endpoint(db, tenant)
        # As many as the account may look up at once (its outbound places):
        # their lookups answer together, and both reach the count.
        from korely_agent.core import threads

        racers = getattr(threads, "OUTBOUND_PER_ACCOUNT", 2)
        barrier = _threading.Barrier(racers)
        monkeypatch.setattr(webhooks, "check_url",
                            lambda u: (barrier.wait(10), u.strip())[1])
        counted = _threading.Barrier(racers)

        class _Ceiling(int):
            """MAX_ENDPOINTS, whose comparison with the count waits until
            every racer has counted (or 2 s, when a lock keeps one from
            counting): the moment a race needs, made certain."""

            def __le__(self, held):
                try:
                    counted.wait(2)
                except _threading.BrokenBarrierError:
                    pass
                return int(self) <= held

        monkeypatch.setattr(webhooks, "MAX_ENDPOINTS", _Ceiling(webhooks.MAX_ENDPOINTS))
        Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        outcomes = []

        def create(i):
            s = Session()
            try:
                webhooks.create_endpoint(s, tenant.id, url=f"https://hooks.example/race{i}",
                                         events=None)
                outcomes.append("created")
            except webhooks.TooManyEndpoints:
                outcomes.append("refused")
            finally:
                s.rollback()
                s.close()

        ts = [_threading.Thread(target=create, args=(i,)) for i in range(racers)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        held = db.query(AgentWebhookEndpoint).filter(
            AgentWebhookEndpoint.tenant_id == tenant.id).count()
        assert sorted(outcomes) == ["created"] + ["refused"] * (racers - 1), outcomes
        assert held == webhooks.MAX_ENDPOINTS
