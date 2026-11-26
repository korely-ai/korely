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

from tests.thread_helpers import held_now, wait_account_idle, wait_idle

from korely_agent.core import threads
from korely_agent.core.times import aware
from korely_agent.db.models import AgentWebhookDelivery, AgentWebhookEndpoint, Tenant
from korely_agent.services import keys as key_service
from korely_agent.services import webhooks



# A Standard Webhooks secret (0.2.0 sends no endpoint with a secret of before 0.1.11).
SECRET = "whsec_AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="  # bytes 0..31, gitleaks:allow

# The product's patience of a round, which the fixture below lengthens.
ROUND_PATIENCE = webhooks._ROUND_PATIENCE
# The product's deadline of a name lookup, which a test below shortens.
LOOKUP_SECONDS = threads.LOOKUP_SECONDS


@pytest.fixture(autouse=True)
def _a_round_waits_for_its_attempts(monkeypatch):
    """A round collects its attempts for `_ROUND_PATIENCE` (1 s) and no
    longer, and the tests that read a round's counts or the outcomes its
    attempts write read them before they were written when a loaded machine
    took longer (the audit of 31 Oct 2026). Here a round waits for every
    attempt it starts, as the older backlogs' test did first. Waiting after
    the round (`wait_idle`) would not do for the tests that read the counts
    it returns, which are the attempts ended within its patience.

    It changes more than the wait: a claim's lease counts the patience
    (`_lease_seconds`, 196.5 s here, 137.5 s in the product), and a round
    hands an attempt back unstarted (`_released`), and says `more` for it,
    only after 60 s with no room for it. The tests whose subject is the
    patience, the lease or a hand-back put the product's back
    (`ROUND_PATIENCE`): the attempt's, the stalling account's and the
    lease's."""
    monkeypatch.setattr(webhooks, "_ROUND_PATIENCE", 60.0)


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
    return (aware(d.next_retry_at) - datetime.now(timezone.utc)).total_seconds()


def _wait_written(db, d, monkeypatch):
    """One round over `d`, and the wait its attempt wrote, as (shortest,
    longest) it can be: the attempt writes `now + wait` inside
    `_write_outcome`, so the moments just before and just after that call
    bound it, however long the rest of the round took on a loaded machine.
    Bounded by the whole round instead, a round of 5 s let a wait of
    nothing pass for the first one, of 5 s. The outcome is waited for
    (`wait_idle`), not left to the round's patience: an attempt that had not
    written when the round returned left its lease to be read (the audit of
    31 Oct 2026)."""
    moments = []
    write = webhooks._write_outcome

    def timed(*a, **k):
        before = datetime.now(timezone.utc)
        try:
            return write(*a, **k)
        finally:
            moments.append((before, datetime.now(timezone.utc)))

    with monkeypatch.context() as m:
        m.setattr(webhooks, "_write_outcome", timed)
        webhooks.process_deliveries(db)
        assert wait_idle(webhooks._sending, 30), "the attempt did not end"
    ((began, ended),) = moments
    db.refresh(d)
    when = aware(d.next_retry_at)
    return (when - ended).total_seconds(), (when - began).total_seconds()


class TestTheSchedule:
    def test_ten_attempts_over_three_days(self):
        assert webhooks.ATTEMPTS == 10
        assert sum(webhooks.SCHEDULE) == 75 * 3600 + 35 * 60 + 5

    def test_each_wait_follows_the_schedule_within_ten_percent(self, db, tenant, monkeypatch):
        monkeypatch.setattr(webhooks, "_deliver", lambda *a, **k: (False, 503, "HTTP 503"))
        _endpoint(db, tenant)
        (d,) = _deliveries(db, tenant, 1)
        assert d.max_attempts == 10
        for scheduled, after in ((5, "the first attempt"), (300, "the second")):
            shortest, longest = _wait_written(db, d, monkeypatch)
            assert longest >= scheduled * (1 - webhooks._JITTER) and \
                shortest <= scheduled * (1 + webhooks._JITTER), \
                f"after {after}: {scheduled} s, written {shortest:.1f} to {longest:.1f} s"
            d.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()


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
        suite). So the round here, as in every test of this file
        (`_a_round_waits_for_its_attempts`), waits for every attempt it
        starts, after the attempts of earlier tests have ended; the patience
        itself is TestAnAttemptHasFifteenSeconds' subject, not this one's."""
        assert wait_idle(webhooks._sending, 30), "an earlier test's attempts still hold the threads"
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
        from korely_agent.core import threads

        monkeypatch.setattr(webhooks, "_turns", threads.Rotation())
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
        """Ordered by events, not timed: the receiver answers only when the
        test lets it, after the round has returned and past the attempt's
        deadline. The round was timed (under 1.5 s, the receiver sleeping
        2 s), and a loaded machine took longer than that over the round's
        own work (the audit of 31 Oct 2026); a round that waited for the
        receiver would hold it until `release` gave up, and it would have
        answered."""
        import threading as _threading

        monkeypatch.setattr(webhooks, "_ROUND_PATIENCE", ROUND_PATIENCE)
        monkeypatch.setattr(webhooks, "_ATTEMPT_SECONDS", 0.3)
        entered, answered, release = _threading.Event(), _threading.Event(), _threading.Event()
        entered_at = []

        def slow(*a, **k):
            entered_at.append(time.monotonic())
            entered.set()
            release.wait(30)
            answered.set()
            return True, 204, None

        monkeypatch.setattr(webhooks, "_deliver", slow)
        _endpoint(db, tenant)
        (d,) = _deliveries(db, tenant, 1)
        try:
            webhooks.process_deliveries(db)
            assert not answered.is_set(), "the round waited for the slow receiver"
            # The receiver answers past its deadline, counted from when it began.
            assert entered.wait(30)
            time.sleep(max(0.0, entered_at[0] + webhooks._give_up_seconds() - time.monotonic()))
        finally:
            # Let go on a failure too: a receiver still held would keep its
            # thread into the next tests.
            release.set()
        # The attempt writes its own outcome when it ends (core/threads.Lanes).
        assert wait_idle(webhooks._sending, 30)
        db.refresh(d)
        assert d.status == webhooks.STATUS_FAILED and "within" in d.last_error


class TestARoundDoesNotWaitForTheSlowestAccount:
    """Audit of 20 Oct 2026: a round waited for its slowest attempt, so an
    account whose receiver held each attempt to its deadline set the pace of
    every account's next round, and its attempts took all eight threads while
    the others' queued."""

    def test_a_round_collects_its_attempts_for_one_second(self):
        """The bound of the tests ordered by events (this class's and
        TestAnAttemptHasFifteenSeconds'): a round returns at its patience,
        before their receivers answer, which they do only when let go or
        after 30 s and more. A patience raised towards that would still pass
        them, so the product's is pinned here, as the schedule's attempts
        are (`test_ten_attempts_over_three_days`)."""
        assert ROUND_PATIENCE == 1.0

    def test_a_stalling_account_holds_its_share_and_the_others_go_out(self, db, tenant,
                                                                      monkeypatch):
        """Ordered by events, not timed. The stalling account has ten
        deliveries, the other two, so the claim takes them in turns S F S F
        S S ... (or F S F S S S ...): the round's first pass, all made under
        the Lanes' condition, starts the other's two and the stalling
        account's share (`_IN_FLIGHT // 2`) and no more, whatever the load,
        where a round that ignored the share gave six of the eight slots to
        stalls in that pass alone. The stalls hold until the test lets them go: after the
        round has returned, the other's deliveries are written and a second
        round has claimed nothing of an account that holds its share; their
        deadline and their hold (`held`) are put past every wait of the test
        before then. The round was timed (under 1.6 s, each stall 1 s), which
        a loaded machine did not keep (the audit of 31 Oct 2026); a round
        that waited for the stalls would hold them until `release` gave up,
        and one would have answered."""
        import threading as _threading

        held = 120.0
        assert wait_idle(webhooks._sending, 30), "an earlier test's attempts still hold the threads"
        monkeypatch.setattr(webhooks, "_ROUND_PATIENCE", ROUND_PATIENCE)
        monkeypatch.setattr(webhooks, "_ATTEMPT_SECONDS", held)
        lock = _threading.Lock()
        release = _threading.Event()
        flight = {"started": 0, "answered": 0}

        def deliver(url, secret, delivery_id, payload):
            if "stalls" not in url:
                return True, 204, None
            with lock:
                flight["started"] += 1
            try:
                release.wait(held)    # held until the test lets it go
                return False, None, "no complete answer"
            finally:
                with lock:
                    flight["answered"] += 1

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
        fast = _deliveries(db, tenant, 2)
        db.commit()
        try:
            webhooks.process_deliveries(db, limit=20)
            assert flight["answered"] == 0, "the round waited for the stalling account"
            assert webhooks._sending.running().get(stalling.id) == webhooks._IN_FLIGHT // 2, \
                "the stalling account took more than its share"
            # The other account's deliveries went out beside the stalls, and the
            # next round leaves the stalling account alone while it holds its share.
            assert wait_account_idle(webhooks._sending, tenant.id, 30)
            for d in fast:
                db.refresh(d)
            assert all(d.status == webhooks.STATUS_DELIVERED for d in fast)
            assert webhooks.process_deliveries(db, limit=20) == {"delivered": 0, "failed": 0}
            assert flight["answered"] == 0, "the others waited for the stalling account"
        finally:
            # Let go on a failure too: stalls held for `held` would keep
            # their threads, and their slots, into the next tests.
            release.set()
        assert wait_idle(webhooks._sending, 30)
        assert flight["started"] == webhooks._IN_FLIGHT // 2, flight
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
        """Two deliveries claimed together, one slot, each attempt with its
        100 s: the claim's lease, read during each attempt that starts,
        still has more than 200 s ahead of it, past the attempt's give-up
        (110 s), so no drain takes a delivery while this one is on it, the
        second waiting its turn included. The lease counts the round's patience
        (`_lease_seconds`), so the product's is put back: the file's 60 s
        made every lease 59 s longer, and a margin cut to 60 s passed. The
        second attempt starts within the patience or not at all: on a loaded
        machine it may run out first, and that delivery goes back to the
        queue unsent with its attempt given back (`_released`), which the
        test checks too. Read once the attempts have ended (`wait_idle`), not
        from the round's counts."""
        from sqlalchemy.orm import sessionmaker

        monkeypatch.setattr(webhooks, "_ROUND_PATIENCE", ROUND_PATIENCE)
        monkeypatch.setattr(webhooks, "_IN_FLIGHT", 1)
        monkeypatch.setattr(webhooks, "_ATTEMPT_SECONDS", 100.0)
        _endpoint(db, tenant)
        rows = _deliveries(db, tenant, 2)
        other = sessionmaker(bind=engine, expire_on_commit=False)
        leases = {}

        def deliver(url, secret, delivery_id, payload):
            with other() as s:
                leases[uuid.UUID(delivery_id)] = _wait(
                    s.get(AgentWebhookDelivery, uuid.UUID(delivery_id)))
            return True, 204, None

        monkeypatch.setattr(webhooks, "_deliver", deliver)
        webhooks.process_deliveries(db)
        assert wait_idle(webhooks._sending, 30), "the attempts did not end"
        for r in rows:
            db.refresh(r)
        sent = [r for r in rows if r.id in leases]
        assert sent, "no attempt started"
        assert all(r.status == webhooks.STATUS_DELIVERED for r in sent), \
            [(r.status, r.last_error) for r in sent]
        assert all(leases[r.id] > 200 for r in sent), leases
        assert all((r.status, r.attempts, r.next_retry_at) == (webhooks.STATUS_PENDING, 0, None)
                   for r in rows if r.id not in leases), \
            "a delivery never started was not given back to the queue"


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
        with webhooks.an_outbound_place(tenant.id), webhooks.an_outbound_place(tenant.id):
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
        with webhooks.an_outbound_place(tenant.id), webhooks.an_outbound_place(tenant.id):
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

    def test_a_lookup_has_five_seconds(self):
        """The product's deadline of a lookup, pinned as the round's patience
        is (`test_a_round_collects_its_attempts_for_one_second`): the next
        test's name answers short of it, and a deadline cut towards the
        0.3 s that test gives would leave no room between the two."""
        assert LOOKUP_SECONDS == 5.0

    def test_a_slow_name_is_given_up_and_still_holds_the_place(self, db, tenant, monkeypatch):
        """Ordered by events, not timed (it was under 2 s, which a loaded
        machine did not keep; the audit of 31 Oct 2026). The name answers a
        public address when the test lets it, or by itself `answers_after`
        seconds after the lookup began: past the deadline the test gives
        (0.3 s) and short of the product's (`LOOKUP_SECONDS`). A create that
        kept the given deadline was refused before the answer; one that
        waited for the product's would have had the address and created the
        endpoint."""
        import socket
        import threading as _threading

        answers_after = LOOKUP_SECONDS * 0.8
        monkeypatch.setattr(threads, "LOOKUP_SECONDS", 0.3)
        release, answered = _threading.Event(), _threading.Event()

        def slow(host, port=None, *a, **k):
            release.wait(answers_after)
            answered.set()
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                     ("93.184.216.34", port or 0))]

        monkeypatch.setattr(socket, "getaddrinfo", slow)
        with pytest.raises(webhooks.UnsafeWebhookUrl) as e:
            webhooks.create_endpoint(db, tenant.id, url="https://h1.slow.invalid/x", events=None)
        assert not answered.is_set(), "the create waited for the slow name"
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
