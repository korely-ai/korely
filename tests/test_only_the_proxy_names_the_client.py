"""Only the proxy in front says where a request came from (core/proxies.py).

Up to 1.0.22 the image believed X-Forwarded-For and X-Forwarded-Proto from
any peer (`FORWARDED_ALLOW_IPS=*`): a container on the compose network, or
port 8000 published by mistake, chose its own address for the login lock,
the LOGIN audit row and the caps per network (the review of 6 Nov 2026; the
founder's decision of 14 Nov 2026: the service trusts its own proxy alone).
The image now names the proxy, `caddy`, and the name is resolved again when
the proxy comes back at another address.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from korely_agent.core import proxies

CADDY = "172.18.0.5"
CADDY_AGAIN = "172.18.0.9"
NEIGHBOUR = "172.18.0.7"
CLIENT = "203.0.113.9"


def _ask(mw, peer, headers=(), settle=True):
    """The scope the application behind `mw` sees for one request; with
    `settle`, once the lookup it may have started is done, so that none
    outlives the test (and calls the next test's resolver)."""
    seen = {}

    async def app(scope, receive, send):
        seen.update(scope)

    mw.app = app
    mw._reader = mw._build()
    scope = {"type": "http", "client": (peer, 40000), "scheme": "http",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers]}
    asyncio.run(mw(scope, None, None))
    if settle:
        _settle(mw)
    return seen


def _settle(mw):
    """Once the lookup in flight is done, and what waited for its answer
    (the log's line) has run: the lookups' one thread runs a job's
    callbacks before it takes the next job, so an empty job is the mark."""
    if mw._job is not None:
        mw._job.result(5)
        mw._pool.submit(lambda: None).result(5)


FORWARDED = (("X-Forwarded-For", CLIENT), ("X-Forwarded-Proto", "https"))


@pytest.fixture
def caddy_at(monkeypatch):
    """`caddy` resolves to what the list holds now; counts the lookups."""
    where = {"caddy": {CADDY}}
    asked = []

    def resolve(name):
        asked.append(name)
        answer = where.get(name, frozenset())
        return answer() if callable(answer) else frozenset(answer)

    monkeypatch.setattr(proxies, "_resolve", resolve)
    monkeypatch.setattr(proxies, "LOOKUP_EVERY_S", 0.0)
    return SimpleNamespace(where=where, asked=asked)


def test_the_proxy_names_the_client_and_the_scheme(caddy_at):
    seen = _ask(proxies.TrustedProxies(None, "caddy"), CADDY, FORWARDED)
    assert seen["client"][0] == CLIENT and seen["scheme"] == "https"
    assert proxies.RELAYED_BY_A_STRANGER not in seen


def test_a_neighbour_on_the_network_does_not_choose_its_address(caddy_at):
    """The hole of `*`: another container of the compose network, or a port
    published by mistake, said who it was."""
    seen = _ask(proxies.TrustedProxies(None, "caddy"), NEIGHBOUR, FORWARDED)
    assert seen["client"][0] == NEIGHBOUR and seen["scheme"] == "http"
    assert seen[proxies.RELAYED_BY_A_STRANGER] is True


def test_the_proxy_back_at_another_address_is_believed(caddy_at):
    """A container's address is given at its creation: an upgrade of the
    proxy's image may give it another. A list of addresses taken at the
    start would then believe nobody, and every client would be the proxy."""
    mw = proxies.TrustedProxies(None, "caddy")
    assert _ask(mw, CADDY, FORWARDED)["client"][0] == CLIENT
    caddy_at.where["caddy"] = {CADDY_AGAIN}
    # Its first request starts the lookup and is not held by it.
    assert _ask(mw, CADDY_AGAIN, FORWARDED, settle=False)["client"][0] == CADDY_AGAIN
    mw._job.result(5)
    assert _ask(mw, CADDY_AGAIN, FORWARDED)["client"][0] == CLIENT
    # Its old address, now anybody's, is no longer believed.
    assert _ask(mw, CADDY, FORWARDED)["client"][0] == CADDY


def test_a_name_that_does_not_resolve_believes_nobody(caddy_at):
    """install.sh on a laptop starts no Caddy: the name resolves to nothing,
    and a caller keeps its own address."""
    caddy_at.where.clear()
    seen = _ask(proxies.TrustedProxies(None, "caddy"), NEIGHBOUR, FORWARDED)
    assert seen["client"][0] == NEIGHBOUR and seen["scheme"] == "http"


def test_after_the_first_lookup_no_request_waits_for_one(caddy_at):
    """A name that answered once, if only "nothing", holds no request again:
    a lookup that hangs keeps its own thread (the review of 14 Nov 2026:
    every stranger's request waited up to 2 s)."""
    import threading

    mw = proxies.TrustedProxies(None, "caddy")
    _ask(mw, NEIGHBOUR, FORWARDED)                  # the first, waited for
    release = threading.Event()
    caddy_at.where["caddy"] = lambda: (release.wait(10), frozenset({CADDY}))[1]
    began = time.monotonic()
    seen = _ask(mw, NEIGHBOUR, FORWARDED, settle=False)
    assert time.monotonic() - began < 0.5 and seen["client"][0] == NEIGHBOUR
    release.set()
    mw._job.result(5)


def test_a_lookup_that_fails_for_a_while_keeps_the_last_answer(caddy_at):
    """A timeout or EAI_AGAIN is not "no such name": Caddy stays believed,
    or every client would get its address (one login lock for all)."""
    mw = proxies.TrustedProxies(None, "caddy")
    assert _ask(mw, CADDY, FORWARDED)["client"][0] == CLIENT
    caddy_at.where["caddy"] = lambda: None
    _ask(mw, NEIGHBOUR, FORWARDED)
    mw._job.result(5)
    assert _ask(mw, CADDY, FORWARDED)["client"][0] == CLIENT
    caddy_at.where["caddy"] = frozenset()           # really gone
    _ask(mw, NEIGHBOUR, FORWARDED)
    mw._job.result(5)
    assert _ask(mw, CADDY, FORWARDED)["client"][0] == CADDY


@pytest.mark.parametrize("errno_name, answer", [("EAI_AGAIN", None), ("EAI_NONAME", frozenset())])
def test_the_resolvers_answers_are_told_apart(monkeypatch, errno_name, answer):
    import socket

    def fails(*a, **k):
        raise socket.gaierror(getattr(socket, errno_name), "test")

    monkeypatch.setattr(socket, "getaddrinfo", fails)
    assert proxies._resolve("caddy") == answer


def test_a_stranger_does_not_make_a_lookup_of_every_request(caddy_at, monkeypatch):
    monkeypatch.setattr(proxies, "LOOKUP_EVERY_S", 3600.0)
    mw = proxies.TrustedProxies(None, "caddy")
    for _ in range(5):
        _ask(mw, NEIGHBOUR, FORWARDED)
    assert caddy_at.asked == ["caddy"]


def test_a_request_without_the_headers_makes_no_lookup(caddy_at):
    """A health check, or install.sh's laptop setup with no Caddy: nothing to
    believe, so nothing to look up."""
    mw = proxies.TrustedProxies(None, "caddy")
    seen = _ask(mw, NEIGHBOUR)
    assert seen["client"][0] == NEIGHBOUR and caddy_at.asked == []
    assert proxies.RELAYED_BY_A_STRANGER not in seen


def test_addresses_and_networks_need_no_lookup(caddy_at):
    mw = proxies.TrustedProxies(None, "10.0.0.7, 192.168.10.0/24")
    assert _ask(mw, "10.0.0.7", FORWARDED)["client"][0] == CLIENT
    assert _ask(mw, "192.168.10.3", FORWARDED)["client"][0] == CLIENT
    assert _ask(mw, NEIGHBOUR, FORWARDED)["client"][0] == NEIGHBOUR
    assert caddy_at.asked == []


def test_the_header_is_read_from_the_right(caddy_at):
    """With a proxy named (not `*`), a client's own entry at the left of an
    appended header is not the address."""
    seen = _ask(proxies.TrustedProxies(None, "caddy"), CADDY,
                (("X-Forwarded-For", f"198.51.100.1, {CLIENT}"),))
    assert seen["client"][0] == CLIENT


def test_unset_is_uvicorns_own_default(caddy_at):
    assert _ask(proxies.TrustedProxies(None, None), "127.0.0.1", FORWARDED)["client"][0] == CLIENT
    assert _ask(proxies.TrustedProxies(None, None), NEIGHBOUR, FORWARDED)["client"][0] == NEIGHBOUR


def test_health_keeps_its_details_from_a_request_a_stranger_relayed():
    """A proxy nobody named relays every client from its own private
    address, which /health would otherwise take for this machine's."""
    from korely_agent.main import from_the_internet

    relayed = SimpleNamespace(scope={proxies.RELAYED_BY_A_STRANGER: True},
                              client=SimpleNamespace(host=NEIGHBOUR))
    direct = SimpleNamespace(scope={}, client=SimpleNamespace(host=NEIGHBOUR))
    assert from_the_internet(relayed) is True
    assert from_the_internet(direct) is False



def _together(mw, *asks):
    """Requests served on one event loop, as a server serves them, each
    (peer, seconds after the first): the scope the application saw for
    each, in the same order, and how long each was held."""
    seen, held = [{} for _ in asks], [None] * len(asks)

    async def app(scope, receive, send):
        seen[scope["test.n"]].update(scope)

    mw.app = app
    mw._reader = mw._build()

    async def one(n, peer, delay):
        await asyncio.sleep(delay)
        began = time.monotonic()
        await mw({"type": "http", "client": (peer, 40000), "scheme": "http", "test.n": n,
                  "headers": [(k.lower().encode(), v.encode()) for k, v in FORWARDED]},
                 None, None)
        held[n] = time.monotonic() - began

    async def main():
        await asyncio.gather(*(one(n, peer, delay) for n, (peer, delay) in enumerate(asks)))

    asyncio.run(main())
    return seen, held


@pytest.fixture
def said(monkeypatch):
    """The log's `[proxy]` lines."""
    lines = []
    monkeypatch.setattr(proxies.logger, "warning", lambda *a, **k: lines.append(a))
    return lines


def test_every_request_while_the_first_lookup_runs_waits_for_it(caddy_at, said):
    """The first request waited for the process's first lookup and the next
    ones did not: a request of the proxy that came while the name was being
    looked up was served as a stranger's (the proxy's address, http) and the
    one-time warning said the proxy was not trusted (found on the Cloud's
    deploy of 14 Nov 2026). Every request that comes before the first lookup
    has answered waits for it."""
    caddy_at.where["caddy"] = lambda: (time.sleep(0.5), frozenset({CADDY}))[1]
    mw = proxies.TrustedProxies(None, "caddy")
    seen, _held = _together(mw, (CADDY, 0), (CADDY, 0.05), (CADDY, 0.1))
    for one in seen:
        assert (one["client"][0], one["scheme"]) == (CLIENT, "https"), one
        assert proxies.RELAYED_BY_A_STRANGER not in one
    assert caddy_at.asked == ["caddy"] and said == []
    # A stranger on the network, after it: served as one, and said once,
    # when the names have answered.
    seen, _held = _together(mw, (NEIGHBOUR, 0), (NEIGHBOUR, 0.01))
    assert [one["client"][0] for one in seen] == [NEIGHBOUR, NEIGHBOUR]
    _settle(mw)
    assert len(said) == 1


def test_the_first_lookup_holds_no_request_past_its_own_deadline(caddy_at, monkeypatch):
    """A resolver that hangs holds each request only until the first lookup's
    deadline (LOOKUP_TIMEOUT_S after it began), not a deadline of its own."""
    import threading

    monkeypatch.setattr(proxies, "LOOKUP_TIMEOUT_S", 0.4)
    release = threading.Event()
    caddy_at.where["caddy"] = lambda: (release.wait(10), frozenset({CADDY}))[1]
    mw = proxies.TrustedProxies(None, "caddy")
    try:
        seen, held = _together(mw, (CADDY, 0), (CADDY, 0.3))
        assert 0.35 < held[0] < 2 and held[1] < held[0], held
        assert [one["client"][0] for one in seen] == [CADDY, CADDY]
        # After the deadline nobody waits, though the lookup still runs.
        _seen, held = _together(mw, (CADDY, 0))
        assert held[0] < 0.3, held
    finally:
        release.set()
        _settle(mw)


def test_the_warning_waits_for_the_answer_about_a_new_address(caddy_at, said):
    """The proxy back at another address is served as a stranger while the
    lookup it started runs (no request waits after the first lookup), and is
    not one: the warning is said only of a peer the names' answer leaves out."""
    mw = proxies.TrustedProxies(None, "caddy")
    assert _ask(mw, CADDY, FORWARDED)["client"][0] == CLIENT
    caddy_at.where["caddy"] = lambda: (time.sleep(0.3), frozenset({CADDY_AGAIN}))[1]
    assert _ask(mw, CADDY_AGAIN, FORWARDED, settle=False)["client"][0] == CADDY_AGAIN
    assert said == []
    _settle(mw)
    assert _ask(mw, CADDY_AGAIN, FORWARDED)["client"][0] == CLIENT
    assert said == []
    caddy_at.where["caddy"] = {CADDY_AGAIN}
    _ask(mw, NEIGHBOUR, FORWARDED)
    assert len(said) == 1 and NEIGHBOUR not in str(said)


@pytest.mark.parametrize("stranger", [NEIGHBOUR, "198.51.100.7"], ids=["before-the-proxy", "after-the-proxy"])
def test_a_stranger_seen_only_while_a_lookup_hangs_is_said_when_it_ends(caddy_at, said, stranger):
    """The line was decided for the first peer a lookup met: the proxy at a
    new address, believed once it answered, and a stranger that came during
    the same lookup was never said (the skeptic's review of 15 Nov 2026).
    Each peer seen meanwhile is judged when the lookup ends, whatever order
    the peers sort in (the re-check of 15 Nov 2026: with the stranger only
    before the proxy, judging the first peer alone passed)."""
    import threading

    mw = proxies.TrustedProxies(None, "caddy")
    assert _ask(mw, CADDY, FORWARDED)["client"][0] == CLIENT
    release = threading.Event()
    caddy_at.where["caddy"] = lambda: (release.wait(10), frozenset({CADDY_AGAIN}))[1]
    _ask(mw, CADDY_AGAIN, FORWARDED, settle=False)
    _ask(mw, stranger, FORWARDED, settle=False)
    assert said == []
    release.set()
    _settle(mw)
    assert len(said) == 1 and _ask(mw, CADDY_AGAIN, FORWARDED)["client"][0] == CLIENT


def test_a_lookup_that_fails_holds_no_request_in_an_error(caddy_at):
    """A lookup that raised, not a resolver's answer, was a 500 to every
    request waiting for the first one (5 of 5 in the skeptic's probe of 15
    Nov 2026): the last answer stands (none, the first time) and the
    requests are served."""
    def raises():
        time.sleep(0.2)
        raise RuntimeError("a lookup that fails")

    mw = proxies.TrustedProxies(None, "caddy")
    mw._look_up_every_name = raises
    seen, _held = _together(mw, (CADDY, 0), (CADDY, 0.05), (NEIGHBOUR, 0.1))
    assert [one["client"][0] for one in seen] == [CADDY, CADDY, NEIGHBOUR]
    assert all(one[proxies.RELAYED_BY_A_STRANGER] for one in seen)
    _settle_quietly(mw)


def _settle_quietly(mw):
    """`_settle`, for a lookup that raised."""
    try:
        mw._job.result(5)
    except RuntimeError:
        pass
    mw._pool.submit(lambda: None).result(5)


def test_any_failure_of_the_resolver_is_no_answer(monkeypatch):
    """Not only the resolver's own errors: whatever it raises keeps the last
    answer, never the request's error."""
    import socket

    def fails(*a, **k):
        raise RuntimeError("not a resolver's error")

    monkeypatch.setattr(socket, "getaddrinfo", fails)
    assert proxies._resolve("caddy") is None
