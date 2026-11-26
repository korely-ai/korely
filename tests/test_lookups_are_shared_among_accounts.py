"""The lookups of webhooks' names are shared among accounts (fixed on
25 Nov 2026; finding of audit 6, 27 Oct 2026,
`korely.threads.lookup-pool-per-account-bound-sums-past-pool`).

core/threads.lookup ran every lookup on one pool of 32 threads with four
per account and no total: eight accounts whose names never answer held the
32 threads, and every other account's lookup queued behind them and timed
out, a webhook's create answering 400 and its deliveries failing. Now the
threads are the accounts' shares summed for LOOKUP_ACCOUNTS, and a lookup
starts on a free thread at once or is refused at once, never queued.

No resolver is asked: getaddrinfo is replaced by one that answers when the
test lets it (the slow names) or at once (the ninth account's)."""
import socket
import threading
import time
import uuid

import pytest

from korely_agent.core import threads
from korely_agent.services import webhooks
from tests.thread_helpers import held_now


def _answer(port):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
             ("93.184.216.34", port or 0))]


@pytest.fixture
def slow_names(monkeypatch):
    """Names ending in .slow.invalid answer when `release` is set; any other
    name answers a public address at once. The callers of the slow ones are
    given a short deadline; the threads they leave are freed at the end."""
    release = threading.Event()

    def resolve(host, port=None, *a, **k):
        if str(host).endswith(".slow.invalid"):
            release.wait(30)
        return _answer(port)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    yield release
    release.set()
    for _ in range(200):
        if not held_now(threads._lookup_threads) and not held_now(threads._lookups):
            break
        time.sleep(0.05)
    assert held_now(threads._lookup_threads) == {}
    assert held_now(threads._lookups) == {}


def _hold(accounts: int, monkeypatch) -> list:
    """`accounts` accounts, each with its LOOKUPS_PER_ACCOUNT lookups of a
    name that does not answer, their callers gone past their deadline: the
    threads stay in the resolver."""
    monkeypatch.setattr(threads, "LOOKUP_SECONDS", 0.2)
    held, errors = [], []

    def one(account, i):
        with threads.for_account(account):
            try:
                threads.lookup(socket.getaddrinfo, f"h{i}.slow.invalid", None)
            except BaseException as e:  # noqa: BLE001 - what each caller got
                errors.append(e)

    callers = []
    for _ in range(accounts):
        account = str(uuid.uuid4())
        held.append(account)
        for i in range(threads.LOOKUPS_PER_ACCOUNT):
            t = threading.Thread(target=one, args=(account, i), daemon=True)
            t.start()
            callers.append(t)
    for t in callers:
        t.join(5)
    assert len(errors) == accounts * threads.LOOKUPS_PER_ACCOUNT
    assert all(isinstance(e, TimeoutError) for e in errors), errors
    assert sum(held_now(threads._lookups).values()) == accounts * threads.LOOKUPS_PER_ACCOUNT
    monkeypatch.setattr(threads, "LOOKUP_SECONDS", 5.0)
    return held


def test_eight_accounts_whose_names_never_answer_leave_a_ninth_its_lookup(slow_names,
                                                                          monkeypatch):
    """The finding's case: eight accounts at their lookups, their names never
    answering. A ninth account's name answers, and its lookup is answered
    well inside its deadline (it waited the whole of it, and failed)."""
    _hold(8, monkeypatch)
    ninth = str(uuid.uuid4())
    started = time.monotonic()
    with threads.for_account(ninth):
        infos = threads.lookup(socket.getaddrinfo, "hooks.example", None)
    assert infos == _answer(None)
    assert time.monotonic() - started < 2.0


def test_a_check_of_a_ninth_accounts_url_is_answered(slow_names, monkeypatch):
    """The same through the check every create, enable and delivery makes
    (services/webhooks.checked_destination)."""
    _hold(8, monkeypatch)
    with threads.for_account(str(uuid.uuid4())):
        url, addresses = webhooks.checked_destination("https://hooks.example/x")
    assert [str(a) for a in addresses] == ["93.184.216.34"]


def test_an_account_holds_its_own_share_and_no_more(slow_names, monkeypatch):
    """An account at its LOOKUPS_PER_ACCOUNT is refused at once for its own
    next lookup (its caller's 429), however many threads are free."""
    held = _hold(1, monkeypatch)
    with threads.for_account(held[0]), pytest.raises(threads.Refused):
        threads.lookup(socket.getaddrinfo, "hooks.example", None)
    assert threads.LOOKUP_THREADS - sum(held_now(threads._lookup_threads).values()) > 0


def test_with_every_thread_held_a_lookup_is_refused_at_once(slow_names, monkeypatch):
    """LOOKUP_ACCOUNTS accounts at their share hold every thread: the next
    account's lookup is refused at once, never queued to wait out its
    deadline, and its check is a name not looked up now (LookupFailed: a
    delivery takes it as a failed attempt and retries on its schedule)."""
    assert threads.LOOKUP_THREADS == threads.LOOKUPS_PER_ACCOUNT * threads.LOOKUP_ACCOUNTS
    _hold(threads.LOOKUP_ACCOUNTS, monkeypatch)
    other = str(uuid.uuid4())
    started = time.monotonic()
    with threads.for_account(other), pytest.raises(threads.LookupsFull):
        threads.lookup(socket.getaddrinfo, "hooks.example", None)
    with threads.for_account(other), pytest.raises(webhooks.LookupFailed,
                                                   match="could not be resolved now"):
        webhooks.checked_destination("https://hooks.example/x")
    assert time.monotonic() - started < 1.0
    # Refused, it holds nothing.
    assert other not in held_now(threads._lookups)


def test_with_every_thread_held_another_accounts_delivery_spends_no_attempt(
        db, tenant, slow_names, monkeypatch):
    """Review of fix/limiti-dns-replaces (25 Nov 2026): the refusal of a
    full server was a failed attempt, so 16 accounts whose names hang, kept
    so through ten attempts, switched off the other accounts' endpoints.
    Here the delivery is at its last attempt: had it been spent, its
    endpoint, which accepted nothing, would be off. It makes no attempt:
    its attempt is given back, the endpoint keeps its failures and stays
    on, and the delivery is due again a few seconds later."""
    from datetime import datetime, timezone

    from korely_agent.core.times import aware
    from korely_agent.db.models import AgentWebhookDelivery, AgentWebhookEndpoint
    from tests.thread_helpers import wait_idle

    ep = AgentWebhookEndpoint(id=uuid.uuid4(), tenant_id=tenant.id,
                              url="https://hooks.example/x",
                              secret="whsec_AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",  # gitleaks:allow
                              events=["memory.created"], active=True)
    db.add(ep)
    db.commit()
    webhooks.emit_event(db, tenant_id=tenant.id, event_type="memory.created",
                        data={"id": "mem_0"})
    (d,) = db.query(AgentWebhookDelivery).filter(AgentWebhookDelivery.tenant_id == tenant.id).all()
    d.attempts = d.max_attempts - 1
    db.commit()
    _hold(threads.LOOKUP_ACCOUNTS, monkeypatch)
    monkeypatch.setattr(webhooks, "_ROUND_PATIENCE", 30.0)
    out = webhooks.process_deliveries(db)
    assert wait_idle(webhooks._sending, 30), "the attempt did not end"
    assert (out["delivered"], out["failed"]) == (0, 0), out
    db.expire_all()
    d = db.get(AgentWebhookDelivery, d.id)
    ep = db.get(AgentWebhookEndpoint, ep.id)
    assert d.attempts == d.max_attempts - 1
    assert d.last_error.startswith("not attempted: host could not be resolved now")
    assert 0 < (aware(d.next_retry_at) - datetime.now(timezone.utc)).total_seconds() \
        <= threads.BUSY_RETRY_SECONDS
    assert ep.active and not ep.failure_count
