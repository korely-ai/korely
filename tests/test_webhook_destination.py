"""A webhook reaches the address the check approved, and only that (review of
2026-10-06; the gap this module's docstring stated).

The check and httpx each looked the name up, so a name answering differently
between the two got through (DNS rebinding). And Python calls some addresses
global that carry a private IPv4 inside: 64:ff9b::a00:1 is 10.0.0.1 through
any NAT64 gateway. Now the name is resolved once, the connection goes to a
checked address with the name kept for the Host header and TLS, and the
address inside an IPv6 one is the one judged. No outside host is contacted:
the receiver is a server on this machine.
"""
import ipaddress
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from korely_agent.services import webhooks


@pytest.mark.parametrize("address", [
    "64:ff9b::a00:1",        # NAT64, 10.0.0.1 inside
    "64:ff9b::808:808",      # NAT64 is refused whatever it carries
    "64:ff9b:1::a00:1",      # local-use NAT64
    "fec0::1",               # site-local, deprecated
    "::ffff:10.0.0.1",       # IPv4-mapped
    "2002:a00:1::1",         # 6to4, 10.0.0.1 inside
    "169.254.169.254",       # the cloud metadata service
    "100.64.0.1",            # carrier-grade NAT
    "127.0.0.1", "::1", "0.0.0.0", "224.0.0.1",
])
def test_refused(address):
    assert webhooks.address_problem(ipaddress.ip_address(address)), address


@pytest.mark.parametrize("address", [
    "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8",
])
def test_allowed(address):
    assert webhooks.address_problem(ipaddress.ip_address(address)) is None, address


def test_a_name_on_a_nat64_address_is_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("64:ff9b::a00:1", 0, 0, 0))])
    with pytest.raises(webhooks.UnsafeWebhookUrl, match="NAT64"):
        webhooks.check_url("https://hooks.example/x")


@pytest.mark.parametrize("errno,now", [(socket.EAI_AGAIN, True), (socket.EAI_NONAME, False)],
                         ids=["temporary", "no-such-name"])
def test_a_name_the_resolver_failed_for_now_is_told_apart(monkeypatch, errno, now):
    """The resolver's temporary failure (EAI_AGAIN) says nothing of the
    name: LookupFailed, still an UnsafeWebhookUrl (a webhook is refused as
    before), so a caller that tells "not now" from "not this" can; a name
    that does not exist is the URL's."""
    def resolve(host, *a, **k):
        raise socket.gaierror(errno, "no answer")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(webhooks.UnsafeWebhookUrl) as e:
        webhooks.check_url("https://hooks.example/x")
    assert isinstance(e.value, webhooks.LookupFailed) is now
    assert ("could not be resolved now" if now else "does not resolve") in str(e.value)


def test_a_name_that_did_not_resolve_in_time_is_a_lookup_failed(monkeypatch):
    from korely_agent.core import threads

    monkeypatch.setattr(threads, "LOOKUP_SECONDS", 0.2)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: time.sleep(1.0) or [])
    with pytest.raises(webhooks.LookupFailed, match="did not resolve within"):
        webhooks.check_url("https://hooks.example/x")


class _Receiver(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        _Receiver.seen.append((self.headers.get("host"), self.rfile.read(length), self.path))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def receiver():
    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    _Receiver.seen.clear()
    yield server.server_address[1]
    server.shutdown()


SECRET = "whsec_" + "A" * 43 + "="


@pytest.fixture
def local_test(monkeypatch):
    """The local-test switches on, and no proxy variable of this machine's."""
    from korely_agent.config import get_settings

    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE_URLS", "true")
    monkeypatch.setenv("WEBHOOK_ALLOW_HTTP", "true")
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _resolved_once(monkeypatch):
    """The name answers 127.0.0.1 once (allowed here by the local-test
    switch) and nothing after: a second lookup fails the test."""
    answers = [[ipaddress.ip_address("127.0.0.1")]]

    def resolve_once(host):
        if not answers:
            raise AssertionError("the name was looked up twice")
        return answers.pop()

    monkeypatch.setattr(webhooks, "_resolve", resolve_once)

    def no_second_lookup(host, *a, **k):
        if host == "hooks.example":
            raise AssertionError("the name was looked up again")
        return socket_getaddrinfo(host, *a, **k)

    socket_getaddrinfo = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", no_second_lookup)


# Variables that say nothing about how an http POST goes out: NO_PROXY alone,
# another scheme's proxy. Any of them used to send the delivery through
# httpx, which looked the name up again (audit of 17 Oct 2026).
@pytest.mark.parametrize("env", [
    {},
    {"NO_PROXY": "localhost"},
    {"no_proxy": "*"},
    {"FTP_PROXY": "http://127.0.0.1:9"},
    {"HTTPS_PROXY": "http://127.0.0.1:9"},          # the URL is http
    {"HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": "hooks.example"},
])
def test_the_delivery_goes_where_the_check_looked(receiver, local_test, monkeypatch, env):
    """Delivered, to the checked address, with the name in the Host header."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _resolved_once(monkeypatch)
    ok, code, err = webhooks._deliver(f"http://hooks.example:{receiver}/in", SECRET,
                                      "msg_1", {"type": "memory.created"})
    assert (ok, code, err) == (True, 204, None)
    assert _Receiver.seen[0][0] == f"hooks.example:{receiver}"


@pytest.mark.parametrize("variable", ["HTTP_PROXY", "ALL_PROXY"])
def test_the_proxy_of_the_scheme_carries_it(receiver, local_test, monkeypatch, variable):
    """The operator's proxy for the URL's scheme, or for all: the proxy is
    given the name, and resolves it itself."""
    monkeypatch.setenv(variable, f"http://127.0.0.1:{receiver}")
    _resolved_once(monkeypatch)
    ok, code, err = webhooks._deliver("http://hooks.example:9/in", SECRET,
                                      "msg_1", {"type": "memory.created"})
    assert (ok, code, err) == (True, 204, None)
    assert _Receiver.seen[0][2] == "http://hooks.example:9/in"


@pytest.mark.cloud
def test_the_cloud_refuses_a_proxy_at_start(monkeypatch):
    """On the service any account registers webhooks, and a proxy resolves
    the name itself: the check would no longer be what is reached. NO_PROXY
    alone carries nothing, and is no reason to stop."""
    from korely_agent import config

    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("KORELY_MODE", "hosted")
    monkeypatch.setenv("NO_PROXY", "localhost")
    config.get_settings.cache_clear()
    try:
        assert config.get_settings().hosted
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
        config.get_settings.cache_clear()
        with pytest.raises(Exception, match="HTTPS_PROXY"):
            config.get_settings()
        monkeypatch.setenv("KORELY_MODE", "self_hosted")
        config.get_settings.cache_clear()
        assert not config.get_settings().hosted
    finally:
        config.get_settings.cache_clear()


# ── an attempt ends by its deadline ─────────────────────────────────────────
# A receiver that answers a byte at a time kept the attempt's socket open as
# long as it liked: each read had its own 10 s, and the round only stopped
# waiting (audit of 17 Oct 2026).

def _dripping(head: bytes, drip: bytes, every: float, during: float):
    """A server on loopback that sends `head`, then `drip` every `every`
    seconds for `during` seconds; (port, the moments its connections ended)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    ended = []

    def serve():
        conn, _ = server.accept()
        try:
            conn.recv(65536)
            conn.sendall(head)
            stop = time.monotonic() + during
            while time.monotonic() < stop:
                time.sleep(every)
                conn.sendall(drip)
        except OSError:
            pass
        finally:
            ended.append(time.monotonic())
            conn.close()
            server.close()

    threading.Thread(target=serve, daemon=True).start()
    return server.getsockname()[1], ended


@pytest.mark.parametrize("head, drip", [
    (b"HTTP/1.1 200 OK\r\n", b"X"),                                        # the headers never end
    (b"HTTP/1.1 200 OK\r\nContent-Length: 1000000000\r\n\r\n", b"x"),  # the body never ends
])
def test_an_attempt_ends_by_its_deadline(local_test, monkeypatch, head, drip):
    monkeypatch.setattr(webhooks, "_ATTEMPT_SECONDS", 1.0)
    port, ended = _dripping(head, drip, every=0.2, during=8)
    started = time.monotonic()
    ok, code, err = webhooks._deliver(f"http://127.0.0.1:{port}/in", SECRET, "msg_1",
                                      {"type": "memory.created"})
    took = time.monotonic() - started
    assert took < 2.5, f"the attempt took {took:.1f} s"
    if head.endswith(b"\r\n\r\n"):
        # The status is what a delivery reads; the body never is.
        assert (ok, code) == (True, 200)
    else:
        assert not ok and "within" in err
    # And the socket is closed: the receiver's next byte finds nobody.
    for _ in range(20):
        if ended:
            break
        time.sleep(0.1)
    assert ended and ended[0] - started < 3.5
