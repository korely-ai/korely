"""Which peer may tell the application where a request came from.

TLS ends at the proxy in front (the shipped Caddy), so what arrives here is
plain HTTP from the proxy's address. Two headers say the rest: the client's
address (`X-Forwarded-For`), which decides the login lock, the address in the
LOGIN audit row and the caps per network, and the scheme
(`X-Forwarded-Proto`), which decides every absolute URL the app builds.
Believed from anybody, they let whoever reaches port 8000 directly choose its
own address.

Up to 1.0.22 the image trusted them from anybody (`FORWARDED_ALLOW_IPS=*`),
safe only while the port was reachable from the proxy alone (the review of
6 Nov 2026; the founder's decision of 14 Nov 2026: the service trusts its
own proxy and nothing else). Now `FORWARDED_ALLOW_IPS` lists the peers
believed, each an address, a network (CIDR) or a NAME, and the image says
`caddy`, the service the shipped compose runs in front.

A name is what makes "the proxy" a rule rather than a guess: on a compose
network a container's address is given at its creation and may change when
it is created again (an upgrade of the proxy's image), while its name stays.
The name is looked up when a peer the last answer did not include sends the
headers, at most once every `LOOKUP_EVERY_S`, on a thread of its own. Every
request that comes while the process's first lookup runs waits for it, until
it answers or `LOOKUP_TIMEOUT_S` after it began (up to 1.0.25 only the
request that started it did, and the proxy's next ones, come during the
lookup, were served as a stranger's: the Cloud's deploy of 14 Nov 2026).
After it, a request is never held by a lookup, and one from a peer not yet
believed is served as a stranger's while the lookup runs. So the proxy back
at another address is believed from its first request after that lookup;
its old address stays believed until such a lookup drops it; and whatever
answers to the name on the network is believed, as the proxy (the review
of 14 Nov 2026). A lookup that times out or fails for a while (EAI_AGAIN)
keeps the last answer; only "no such name" empties it, and a name that
never resolved trusts nobody: the request keeps the peer's own address and
scheme.

The headers are read by uvicorn's own middleware, built with the addresses
believed now (`ProxyHeadersMiddleware(app, trusted_hosts=...)`, its public
constructor), so the reading is uvicorn's: the address is taken from the
right of `X-Forwarded-For`, skipping the peers believed, as with any list
that is not `*` (uvicorn 0.52). The image starts uvicorn with
`--no-proxy-headers`: this is the one reader.

A request that carries the headers from a peer that is not believed is a
request relayed by somebody this install does not know: it is marked
(`RELAYED_BY_A_STRANGER`), and /health treats it as coming from the internet
(main.from_the_internet), since its true origin is not known. The log says so
once per process, with the setting to change, of a peer the names' answer
leaves out: while a lookup runs, the proxy at a new address is not known to
be a stranger yet, and the line waits for the answer.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Iterable, Optional

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

logger = logging.getLogger(__name__)

# What the application trusts when FORWARDED_ALLOW_IPS is not set at all:
# uvicorn's own default, so a process started by hand behaves as uvicorn
# would. The image sets `caddy` (Dockerfile).
DEFAULT = "127.0.0.1"

# How often a peer the last answer did not include may ask the names again.
# Every request of a peer that is not the proxy (a health check from this
# machine) would otherwise be a lookup.
LOOKUP_EVERY_S = 5.0
# How long after it began the process's first lookup is waited for, by
# every request that comes meanwhile. One that takes longer goes on on its
# thread, and the last answer (none, the first time) stands.
LOOKUP_TIMEOUT_S = 2.0

# The scope key of a request relayed by a peer that is not believed.
RELAYED_BY_A_STRANGER = "korely.relayed_by_a_stranger"

# How many peers not believed, seen while a lookup runs, are judged once it
# has answered (`_say_once`): enough for the proxy at a new address and the
# strangers beside it; past it, a peer is judged at its next request.
PENDING_MOST = 32

_FORWARDING = (b"x-forwarded-for", b"x-forwarded-proto")


def _entries(spec: Optional[str]) -> list[str]:
    return [e.strip() for e in (DEFAULT if spec is None else spec).split(",") if e.strip()]


def _is_a_name(entry: str) -> bool:
    """A host name to resolve: not `*`, not an address or a network, not one
    of uvicorn's literals (`unix:...`)."""
    if entry == "*" or entry.startswith("unix:"):
        return False
    try:
        ipaddress.ip_network(entry, strict=False)
        return False
    except ValueError:
        return True


# The resolver's answers that mean the name is not there, as against one
# that could not be had now (EAI_AGAIN, a timeout): only these empty it.
_NO_SUCH_NAME = {getattr(socket, n) for n in ("EAI_NONAME", "EAI_NODATA") if hasattr(socket, n)}


def _resolve(name: str) -> Optional[frozenset]:
    """The addresses `name` resolves to now; empty when there is no such
    name; None when no answer could be had (try again later, keep the last)."""
    try:
        return frozenset(info[4][0] for info in
                         socket.getaddrinfo(name, None, type=socket.SOCK_STREAM))
    except socket.gaierror as e:
        return frozenset() if e.errno in _NO_SUCH_NAME else None
    except Exception:
        # Any other failure is no answer either: the last one stands (the
        # skeptic's review of 15 Nov 2026: a lookup that raised was a 500 to
        # every request waiting for it).
        return None


class TrustedProxies:
    """ASGI middleware: uvicorn's reading of the forwarding headers, from the
    peers `spec` names (FORWARDED_ALLOW_IPS), names looked up as they are
    needed."""

    def __init__(self, app, spec: Optional[str] = None) -> None:
        self.app = app
        entries = _entries(spec)
        self.names = tuple(e for e in entries if _is_a_name(e))
        self.fixed = tuple(e for e in entries if not _is_a_name(e))
        self._by_name: dict = {}
        self._lock = threading.Lock()
        self._pool: Optional[ThreadPoolExecutor] = None
        self._job: Optional[Future] = None
        self._next_lookup = 0.0
        # The process's first lookup, and when its requests stop waiting.
        self._first: Optional[Future] = None
        self._first_ends = 0.0
        # The lookup whose answer the log's line waits for, and the peers it
        # judges then (`_say_once`).
        self._said_after: Optional[Future] = None
        self._pending: set = set()
        self._said = False
        self._reader = self._build()

    @property
    def addresses(self) -> frozenset:
        return frozenset().union(*self._by_name.values()) if self._by_name else frozenset()

    def _build(self) -> ProxyHeadersMiddleware:
        # An empty list trusts nobody.
        return ProxyHeadersMiddleware(self.app, trusted_hosts=[*self.fixed, *sorted(self.addresses)])

    def _believed(self, host: Optional[str]) -> bool:
        return bool(host) and host in self._reader.trusted_hosts

    def _look_up_every_name(self) -> None:
        """On the lookup's own thread: each name's answer, the last good
        one kept where none could be had, and the reader rebuilt when what
        is believed changed."""
        found = {name: _resolve(name) for name in self.names}
        with self._lock:
            before = self.addresses
            for name, answer in found.items():
                if answer is not None:
                    self._by_name[name] = answer
            if self.addresses != before:
                self._reader = self._build()

    def _start_a_lookup(self) -> Optional[Future]:
        """The lookup in flight, a new one if none is and the last began
        `LOOKUP_EVERY_S` ago or more; on a pool of one thread, so a resolver
        that hangs holds that thread alone, and never two lookups."""
        if self._job is not None and not self._job.done():
            return self._job
        now = time.monotonic()
        if now < self._next_lookup:
            return None
        self._next_lookup = now + LOOKUP_EVERY_S
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="korely-proxy-lookup")
        self._job = self._pool.submit(self._look_up_every_name)
        return self._job

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        client = scope.get("client")
        host = client[0] if client else None
        # Without the headers whom to believe does not matter: no lookup
        # for a health check or a client that reaches the port directly.
        forwarding = any(n in _FORWARDING for n, _ in scope.get("headers") or ())
        if forwarding and self.names and host and not self._believed(host):
            job = self._start_a_lookup()
            if self._first is None and job is not None:
                self._first, self._first_ends = job, time.monotonic() + LOOKUP_TIMEOUT_S
            # The process's first: the proxy's first requests are its, all of
            # those that come before it answers (one event loop: no two
            # requests read `_first` between its test and its setting).
            left = self._first_ends - time.monotonic()
            if self._first is not None and not self._first.done() and left > 0:
                try:
                    await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(self._first)), left)
                except Exception:
                    # Too slow, or failed: the last answer stands, and the
                    # request goes on (never a 500 of the lookup's).
                    pass
        if forwarding and not self._believed(host):
            scope[RELAYED_BY_A_STRANGER] = True
            self._say_once(host)
        return await self._reader(scope, receive, send)

    def _say_once(self, host: str) -> None:
        """The log's one line of the process, for a peer the names' answer
        leaves out. While a lookup runs the answer is not in yet (the proxy
        at a new address is not a stranger): the peer waits with the others
        seen meanwhile (up to PENDING_MOST), and each is judged when the
        lookup has ended, answered or not (the skeptic's review of 15 Nov
        2026: only the first was, and a stranger seen only during a lookup
        that hung was never said)."""
        job, later = self._job, None
        with self._lock:
            if self._said:
                return
            if job is not None and not job.done():
                if len(self._pending) < PENDING_MOST:
                    self._pending.add(host)
                if self._said_after is job:
                    return
                self._said_after = later = job
            elif self._believed(host):
                return
            else:
                self._said = True
        if later is not None:
            # Outside the lock: a lookup done meanwhile runs this at once.
            later.add_done_callback(lambda _done: self._judge_the_pending())
            return
        logger.warning(
            "[proxy] a request carried X-Forwarded-For or X-Forwarded-Proto from a "
            "peer this install does not trust, so its own address and scheme were "
            "kept. If a proxy of yours is in front, name it in FORWARDED_ALLOW_IPS "
            "(docs/guide/operations.md, \"Your own proxy in front\"); trusted now: %s",
            ", ".join(self.trusted()) or "nobody")

    def _judge_the_pending(self) -> None:
        """Once a lookup has ended: each peer seen while it ran, judged on
        what is believed now (or kept for the lookup that runs now)."""
        with self._lock:
            pending, self._pending = self._pending, set()
        for host in sorted(pending):
            self._say_once(host)

    def trusted(self) -> Iterable[str]:
        """What is believed now: the fixed entries, then the names with what
        they last resolved to."""
        return [*self.fixed, *self.names, *sorted(self.addresses)]
