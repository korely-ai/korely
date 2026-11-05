"""Every resource the accounts of one process share, bounded in one place.

The image runs one uvicorn worker, so one process serves every account: its
request threads, its thread pools, its database connections and the locks
their transactions take, the local embedder's one slot, the threads of the
worker's rounds. Each audit found one more of them that one account could
hold while every other account waited: the loop's default pool and the
embedder's questions (19 Oct 2026); the embedder's documents, the MCP's door
on a cancelled call, the request threads a webhook's name lookup holds, a row
lock waited on with no end, the webhook delivery round and the facts drain's
round (20 Oct 2026). So since 20 Oct 2026 every one of them takes its
per-account cap and its deadline from this module, and
tests/test_shared_resources_are_bounded_here.py fails on a pool, a semaphore,
a condition, an engine or a database deadline made anywhere else:

- a request's thread (`on_a_request`): an account has at most
  `CALLS_PER_ACCOUNT` of them waiting on shared work, the embedder's slot
  (core/local_embedder._Turns), and none of them waits there more than
  `WAIT_SECONDS`; a read that needs a question's vector waits for one of
  those places on the loop before it takes a thread
  (`waits_for_the_embedder`, 31 Oct 2026), any other request's vector takes
  one at once or goes without (`at_the_embedder`);
- the tools' bodies' pool (`tool_body`), where an MCP tool's body runs: the
  MCP's door keeps an account's place, and one of every account's
  (`Gates`, fair among accounts), until the body has ended, cancelled or not;
- the admission pool (`admission_off_the_loop`): the work that decides
  whether a request is let in, never behind a tool's body;
- an account's outbound waits on a request thread (`Places`,
  `OUTBOUND_PER_ACCOUNT`), and a name lookup (`lookup`), which has
  `LOOKUP_SECONDS` and holds the account's place until it ends;
- the database (`engine_pool`, `own_engine_pool`, `with_deadlines`): the
  pools' sizes and checkout wait written down, a session opened while
  another may be held taken from the own sessions' pool, never a second of
  the main one, and a request's transaction gives up a lock
  wait after `LOCK_WAIT_SECONDS` and a statement after `STATEMENT_SECONDS`
  (503 `unavailable`, retry: `deadline_passed`), longer only where an
  erasure says so (`longer_deadlines`);
- a worker's rounds (`Lanes`): its threads shared among accounts in time as
  well as in count, a round never waiting for its slowest account;
- every thread pool (`pool`).

The rate-limit buckets are rows of the database, not of this process
(services/rate_limit.py); the control plane has its own there.

Two pools for awaited work, since the audit of 19 Oct 2026. A tool's body
goes to the tools' bodies' pool (`tool_body`; the loop's default pool until
the audit of 27 Oct 2026), where a read can wait seconds for its turn at the local
embedder. The work that decides whether a
request is let in, the key check, the per-call counter, OAuth's provider and
the pre-route key lookup, goes to a pool of its own
(`admission_off_the_loop`): on the default pool it queued behind every tool
body of every account, and one key's 40 concurrent searches took another
account's key check from 1.3 ms to 1.5 s. The same remedy the worker's ticks
got on 18 Oct 2026 (worker._pool_of). Here, not in mcp/server.py, since the audit
of 17 Oct 2026: the OAuth service imported it from the MCP's door, a service
depending on a module of the surface above it.
"""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import contextlib
import contextvars
import functools
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


#: How many of one account's calls may hold a thread at once while they wait
#: on work every account shares: the local embedder's one slot
#: (core/local_embedder._Turns), where a read waits seconds for its turn, and
#: a write's vector as much (audit of 20 Oct 2026). A thread that waits there is a
#: thread no other account's request can have, and the embedder's fairness
#: among accounts reaches only callers that already hold one (audit of
#: 19 Oct 2026). One rule, kept at two doors: the MCP's door lets a fifth
#: tool call of an account wait on the event loop, holding no thread
#: (mcp/server.MAX_RUNNING_CALLS_PER_TENANT). And since 31 Oct 2026 one
#: count of the account's places at the embedder for both doors
#: (`_embedder`): a read that needs a question's vector, REST's or the
#: MCP's, waits for its place on the loop before it takes a thread
#: (`waits_for_the_embedder`), and a request already on its thread (a
#: write's vector) takes one at once or goes without (local_embedder.Busy: the
#: write is stored and its vector computed by the worker). Until then a
#: REST search, a sync route already on its thread, was refused at once past
#: four, and REST's four and the MCP's four added up at the embedder (the
#: finding of scripts/concurrent_agents.sh, `_embedder`). Four leaves other
#: accounts half of the tools' bodies' threads (`TOOL_BODY_THREADS`), and
#: more would buy one account little: the embedder runs one inference at a
#: time.
CALLS_PER_ACCOUNT = 4

#: The longest a request's thread waits for its turn at work every account
#: shares (the embedder's slot): under the pool's checkout wait
#: (`POOL_CHECKOUT_SECONDS`), so a request that waits here, holding a pooled
#: connection, gives it back before another account's request waiting for a
#: connection fails. Past it the caller is refused (`Busy`), as past its
#: account's places (audit of 20 Oct 2026: a waiting write had no
#: deadline). And the longest a read waits on the loop for its account's
#: place at the embedder (`waits_for_the_embedder`, 31 Oct 2026), holding
#: neither a thread nor a connection; past it the read goes on without, and
#: is answered as a refused one.
WAIT_SECONDS = 20.0

#: The threads of the MCP tools' bodies (`tool_body`), a pool of their own
#: of a fixed size, and how many bodies of every account together may hold
#: one: the MCP's door's total (mcp/server, `Gates(..., total=...)`), each
#: freed place going to the waiting account with the fewest bodies running.
#: The audit of 27 Oct 2026: the bodies ran on the loop's default pool, min(32, CPUs + 4) threads with
#: one FIFO queue, and the door's bound was per account only, so two accounts
#: at their four filled a one-CPU host's five and a third account's call
#: waited behind their whole backlog. Eight: twice an account's bound, so one
#: account never fills it, and under the main database pool's fifteen
#: connections, which REST's requests and the worker share with them.
TOOL_BODY_THREADS = 8
TOOL_BODIES_AT_ONCE = TOOL_BODY_THREADS

#: The threads of the admission work (`admission_off_the_loop`). Each job is
#: a few milliseconds of database work, never a model call and never a wait
#: on another account, so a few threads serve hundreds of requests a second;
#: each also holds a database connection while it runs, out of the engine's
#: pool (`engine_pool`), which more threads here would only queue on.
ADMISSION_THREADS = 4
#: The longest an admission job waits for another transaction's lock, then
#: gives up as a request does past `LOCK_WAIT_SECONDS` (503 `unavailable`,
#: retry). The audit of 27 Oct 2026: a key check waited a request's 5 s on its key's row, which
#: the account's own project deletion or erasure held, and four of them held
#: the four threads every account's key check shares. The key's stamp no
#: longer waits at all (services/keys.verify_api_key skips a held row); this
#: bounds whatever else an admission job locks (OAuth's project, the
#: counters' rows, a few milliseconds each when nothing long holds them).
ADMISSION_LOCK_WAIT_SECONDS = 0.5

#: The thread that encodes a value nested deeper than the input rule's own
#: stack has room for (services/limits._encoded): it starts with an empty
#: stack. No per-account bound: only a crafted value that deep reaches it,
#: and each job is one C encoding of one value, some 0.3 s for the largest a
#: batch can carry (review of 5a38d9b); ordinary checks never wait on it.
ENCODER_THREADS = 1

# ── the database ──────────────────────────────────────────────────────────
#: The engine's pool, SQLAlchemy's defaults written down so the numbers the
#: rest of the code reasons with are a decision: 5 connections kept, 10 more
#: when they are busy, and a request that finds all 15 checked out waits 30 s
#: for one and then fails (core/auth.credential_lookup: 503 `unavailable`).
POOL_SIZE = 5
POOL_OVERFLOW = 10
POOL_CHECKOUT_SECONDS = 30.0
#: The own sessions' pool (`own_engine_pool`, db/base.get_own_sessionmaker):
#: the short sessions a piece of work opens while it may hold a connection
#: of the main pool, the trail's row (services/audit.record), the Cloud's
#: counts (hosted/quotas), the model gate's read (hosted/spending), the
#: costs (services/llm_usage), the provider's settings (core/llm). The audit
#: of 27 Oct 2026: they took their connection from the main pool, a /v1 read held
#: one while its trail row asked for a second, and fifteen reads at once held
#: all fifteen while each waited for another's, until the checkout gave up
#: and every row was lost. From a pool of their own, which the main pool's
#: holders wait on and its own holders never do (each holds one, and asks
#: for nothing else while it does), the wait always ends. Each is a few
#: statements; a trail row waits at most a request's lock wait for an open
#: erasure (services/audit._settle).
OWN_POOL_SIZE = 5
OWN_POOL_OVERFLOW = 5

#: A request's transaction waits this long for another transaction's lock,
#: then gives up (Postgres `lock_timeout`, SQLSTATE 55P03): 503 `unavailable`
#: with Retry-After, its thread and its connection back. The audit of 20 Oct
#: 2026: a project's erasure holds the
#: project's row for its whole length, every write into the project waited
#: on it with no end, each holding a request thread and a pooled connection,
#: and fifteen of them took the pool every account shares.
LOCK_WAIT_SECONDS = 5
#: And a request's statement this long (`statement_timeout`, SQLSTATE 57014).
STATEMENT_SECONDS = 60
#: The deadlines of a transaction that is long by its nature and is its
#: account's own: an erasure, which waits for the writes under way to commit
#: and then deletes everything it names (`longer_deadlines`).
LONG_LOCK_WAIT_SECONDS = 60
LONG_STATEMENT_SECONDS = 900
#: What a refusal for a deadline tells the caller to wait before asking again.
BUSY_RETRY_SECONDS = 5

# Postgres's SQLSTATE for a lock wait past `lock_timeout` (lock_not_available)
# and a statement past `statement_timeout` (query_canceled).
_DEADLINE_CODES = frozenset({"55P03", "57014"})

# ── outbound waits ────────────────────────────────────────────────────────
#: How many of one account's requests may wait on something outside the
#: process at once, each on a request thread: a webhook's test delivery, the
#: lookup of a webhook's host when it is created or enabled. The Webhooks
#: page sends one at a time; a third waits for nothing it needs. Past it the
#: request is refused at once (429 with Retry-After): a request already on a
#: thread does not wait for a place (the audits of 18 Oct 2026 for the test
#: and of 20 Oct 2026 for the lookups).
OUTBOUND_PER_ACCOUNT = 2
#: How long a name lookup is waited for (`lookup`). getaddrinfo has no
#: timeout of its own: the resolver's chain (the stub's 5 s and attempts,
#: Docker's DNS, the upstream) decides, and a name server that answers
#: slowly or not at all is the caller's to choose.
LOOKUP_SECONDS = 5.0
#: Lookups of one account under way at once, those whose caller stopped
#: waiting included: the threads of `lookup` are shared.
LOOKUPS_PER_ACCOUNT = CALLS_PER_ACCOUNT
LOOKUP_THREADS = 32


# ── whose work this is ───────────────────────────────────────────────────
_ON_A_REQUEST: ContextVar[bool] = ContextVar("korely_on_a_request", default=False)
# Whether this work is an admission job (`admission_off_the_loop`).
_ADMITTING: ContextVar[bool] = ContextVar("korely_admitting", default=False)
# The account the work in this context is done for, where a bound needs it
# and nothing else names it (a lookup inside a webhook's attempt).
_ACCOUNT: ContextVar[Any] = ContextVar("korely_shared_account", default=None)
# The place the work in this context holds (`Places.held`): work it starts
# that outlives it holds the place on (`lookup`).
_PLACE: ContextVar[Optional["Place"]] = ContextVar("korely_shared_place", default=None)
# The event loop the request came in on, for a request's thread that takes a
# place kept on it (`at_the_embedder`); None outside a request.
_LOOP: ContextVar[Optional[asyncio.AbstractEventLoop]] = ContextVar("korely_request_loop",
                                                                    default=None)


def serving_a_request() -> None:
    """Mark this context as a request's: the outermost middleware calls it
    for every HTTP request (main._RememberTheRequest). The context is copied
    to the request's thread (a sync route, a sync dependency), to an MCP
    tool's body (`off_the_loop`) and to the admission pool, and never reaches
    the worker, whose loops start at the process's start. Called on the
    loop, it also keeps the loop, where a request's thread takes a place of
    the ones the loop keeps (`at_the_embedder`)."""
    _ON_A_REQUEST.set(True)
    try:
        _LOOP.set(asyncio.get_running_loop())
    except RuntimeError:
        pass


def on_a_request() -> bool:
    """Whether this work is a request's, holding one of the threads and
    connections every account's requests share: the work the per-account
    caps and the deadlines here are for. The worker's own work is fair by
    its claims and holds none of them."""
    return _ON_A_REQUEST.get()


# ── thread pools ──────────────────────────────────────────────────────────
_pools: Dict[str, ThreadPoolExecutor] = {}
_pools_lock = threading.Lock()


def pool(name: str, threads: int) -> ThreadPoolExecutor:
    """The process's pool called `name`, made on first use with `threads`
    threads named after it, and kept: every pool of the process is made
    here, so each is one line of the inventory this module keeps."""
    with _pools_lock:
        p = _pools.get(name)
        if p is None:
            p = _pools[name] = ThreadPoolExecutor(max_workers=threads, thread_name_prefix=name)
        return p


async def off_the_loop(fn, *args):
    """`fn(*args)` on the loop's default pool, awaited as `tool_body` awaits
    it: the awaited work no door bounds (the Cloud's billing webhooks)."""
    return await _awaited(None, fn, args)


async def tool_body(fn, *args):
    """Run a synchronous piece of a tool in a thread of the tools' bodies'
    pool (`TOOL_BODY_THREADS`), never the loop's default one.

    The image runs one uvicorn worker, so the event loop is the whole server:
    REST, MCP, the screen and `/health`. Every MCP tool does synchronous
    database work, and three of them call the model provider for an embedding
    (korely_add also extracts facts inline when WRITE_ASYNC is off). Done on
    the loop, one agent writing a memory over MCP froze every other request
    for the length of a model call. The context is copied, as
    `asyncio.to_thread` copies it, so the tenant and project the MCP's
    verifier put in its ContextVars, and the network OAuth's doors set, are
    seen in the thread. tests/test_the_event_loop_stays_free.py measures it.

    How many of one account's tool bodies wait here at once is bounded by the
    MCP's door (mcp/server.MAX_RUNNING_CALLS_PER_TENANT, `CALLS_PER_ACCOUNT`),
    and how many of every account's by its total (`TOOL_BODIES_AT_ONCE`), so
    a body the door lets in finds a thread free. A thread cannot be
    cancelled, so a cancelled call keeps its caller, and with it its place at
    that door, until the body has ended, then raises the cancellation: until
    the audit of 20 Oct 2026 the place was given back at once, the account's
    next call took it, and one burst of cancelled writes put five of its bodies in the pool, past its four.
    """
    return await _awaited(pool("tool-body", TOOL_BODY_THREADS), fn, args)


async def _awaited(executor, fn, args):
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    job = loop.run_in_executor(executor, functools.partial(ctx.run, fn, *args))
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        await _until_it_ends(job)
        raise


async def _until_it_ends(job) -> None:
    """Wait for a thread's job to end, whatever cancels the waiter meanwhile:
    shielded from an anyio scope (the MCP SDK cancels a call's scope, and
    anyio cancels again on every step until the task leaves it) and from a
    plain task.cancel()."""
    import anyio

    with anyio.CancelScope(shield=True):
        while not job.done():
            try:
                await asyncio.wait({job})
            except asyncio.CancelledError:
                continue
    if not job.cancelled():
        job.exception()   # retrieved: the caller is cancelled, the outcome goes nowhere


async def admission_off_the_loop(fn, *args):
    """Run the synchronous work that admits a request in a thread of its own
    pool, never behind a tool's body: the MCP's key check and its counters
    (mcp/server.py), OAuth's provider and its per-network count
    (services/oauth.py, api/oauth.py), the pre-route key lookup of a large
    body (main._writer_account).

    The context is copied as `asyncio.to_thread` copies it, so the ContextVars
    set on the loop (the tenant, the project, OAuth's network) are seen in the
    thread. tests/test_one_account_leaves_the_others_a_thread.py holds it.
    """
    ctx = contextvars.copy_context()
    return await asyncio.get_running_loop().run_in_executor(
        pool("admission", ADMISSION_THREADS), functools.partial(ctx.run, _admitting, fn, *args))


def _admitting(fn, *args):
    """`fn(*args)` as an admission job: its transactions have the admission
    jobs' lock wait (`ADMISSION_LOCK_WAIT_SECONDS`, `with_deadlines`)."""
    _ADMITTING.set(True)
    return fn(*args)


async def on_its_pool(executor: ThreadPoolExecutor, fn, *args):
    """`fn(*args)` on `executor`, one of `pool`'s, awaited: a worker job's
    tick on its own thread (worker._loop), never on a pool the doors wait
    on (the loop's default one, the tools' bodies')."""
    return await asyncio.get_running_loop().run_in_executor(executor, fn, *args)


# ── places per account ────────────────────────────────────────────────────

class _Gate:
    """One key's places: how many are held, how many callers hold or wait
    for one (the gate lives while that is not zero), and the waiters, oldest
    first."""

    __slots__ = ("held", "users", "waiters")

    def __init__(self) -> None:
        self.held = 0
        self.users = 0
        self.waiters: Any = collections.deque()


class Gates:
    """Places per key, waited for on the event loop, holding no thread: a
    caller past its key's `per_key` waits its turn among its key's callers,
    first come first served; past `total`, when there is one, for every key
    together, a freed place
    goes to the waiting key that holds the fewest, the oldest waiter on a
    tie, so a key with nothing running gets the next one ahead of every other
    key's backlog (fair queueing, Demers, Keshav and Shenker, SIGCOMM 1989, as
    core/threads.Lanes shares a worker's threads; audit of 27 Oct 2026: a
    FIFO total served the backlog of the keys at their bound first). Each key's gate lives while
    somebody holds it or waits on it, so the tables hold the keys in use now,
    never every key that ever came. Touched only on the event loop.

    The MCP's door (mcp/server, an account's tool calls), the large
    bodies' places (main._BodyCap, an account's uploads past the common
    ceiling) and an account's places at the embedder (`waits_for_the_embedder`)
    are these; the first two were two copies of the same table until the
    audit of 20 Oct 2026.

    A place is also taken at once or not at all (`take_now`, given back with
    `give_back`), for a caller that must not wait: a request's write already
    on its thread, which goes on without its vector (`at_the_embedder`). The
    count per key is kept here rather than in an asyncio.Semaphore since
    31 Oct 2026, so that such a taker and the waiters draw on one number, and
    a taker never goes ahead of a waiter."""

    def __init__(self, per_key: int, total: Optional[int] = None) -> None:
        self.per_key = per_key
        self.total = total
        self._gates: Dict[Any, _Gate] = {}
        # The total's places: how many are held, by key, and the waiters by
        # key, oldest first, each (order of arrival, future).
        self._in_total = 0
        self._held: Dict[Any, int] = {}
        self._waiting: Dict[Any, Any] = {}
        self._arrivals = 0

    def __len__(self) -> int:
        return len(self._gates)

    def _enter(self, key: Any) -> _Gate:
        gate = self._gates.get(key)
        if gate is None:
            gate = self._gates[key] = _Gate()
        gate.users += 1
        return gate

    def _leave(self, key: Any) -> None:
        gate = self._gates[key]
        gate.users -= 1
        if not gate.users:
            del self._gates[key]

    @contextlib.asynccontextmanager
    async def place(self, key: Any):
        """One of `key`'s places, then one of every key's, waited for."""
        await self.take(key)
        try:
            yield
        finally:
            self.give_back(key)

    async def take(self, key: Any) -> None:
        """One of `key`'s places, then one of every key's, waited for, and
        kept until `give_back`: for a holder that gives it back before its
        block ends (`waits_for_the_embedder`). A caller cancelled while it
        waits holds nothing."""
        mine = self._enter(key)
        try:
            await self._take_mine(mine)
        except BaseException:
            self._leave(key)
            raise
        if self.total is None:
            return
        try:
            await self._take_total(key)
        except BaseException:
            self._give_mine(mine)
            self._leave(key)
            raise

    def take_now(self, key: Any) -> bool:
        """One of `key`'s places (and one of every key's) at once, or False
        and nothing taken: when the key holds all of its places, when one of
        its callers waits for one (a taker never goes ahead of a waiter), or
        when every key's are held or waited for. Given back with
        `give_back`."""
        gate = self._gates.get(key)
        if self.per_key <= 0 or (gate is not None and (
                gate.held >= self.per_key or any(not w.done() for w in gate.waiters))):
            return False
        if self.total is not None and (self._in_total >= self.total or self._waiting):
            return False
        gate = self._enter(key)
        gate.held += 1
        if self.total is not None:
            self._grant(key)
        return True

    def give_back(self, key: Any) -> None:
        """A place `take` or `take_now` gave."""
        if self.total is not None:
            self._give_total(key)
        self._give_mine(self._gates[key])
        self._leave(key)

    async def _take_mine(self, gate: _Gate) -> None:
        if gate.held < self.per_key and not gate.waiters:
            gate.held += 1
            return
        waiter = asyncio.get_running_loop().create_future()
        gate.waiters.append(waiter)
        try:
            await waiter
        except BaseException:
            if waiter.done() and not waiter.cancelled():
                self._give_mine(gate)     # granted as its caller was cancelled
            else:
                with contextlib.suppress(ValueError):
                    gate.waiters.remove(waiter)
            raise

    def _give_mine(self, gate: _Gate) -> None:
        gate.held -= 1
        while gate.waiters and gate.held < self.per_key:
            waiter = gate.waiters.popleft()
            if waiter.done():
                continue
            gate.held += 1
            waiter.set_result(None)

    async def _take_total(self, key: Any) -> None:
        if self._in_total < self.total and not self._waiting:
            self._grant(key)
            return
        waiter = asyncio.get_running_loop().create_future()
        self._arrivals += 1
        self._waiting.setdefault(key, collections.deque()).append((self._arrivals, waiter))
        try:
            await waiter
        except BaseException:
            if waiter.done() and not waiter.cancelled():
                self._give_total(key)     # granted as its caller was cancelled
            else:
                queue = self._waiting.get(key)
                if queue is not None:
                    for item in list(queue):
                        if item[1] is waiter:
                            queue.remove(item)
                    if not queue:
                        del self._waiting[key]
            raise

    def _grant(self, key: Any) -> None:
        self._in_total += 1
        self._held[key] = self._held.get(key, 0) + 1

    def _give_total(self, key: Any) -> None:
        self._in_total -= 1
        left = self._held.get(key, 1) - 1
        if left > 0:
            self._held[key] = left
        else:
            self._held.pop(key, None)
        while self._in_total < self.total and self._waiting:
            # The waiting key holding the fewest, then the oldest waiter.
            nxt = min(self._waiting, key=lambda k: (self._held.get(k, 0), self._waiting[k][0][0]))
            queue = self._waiting[nxt]
            _arrival, waiter = queue.popleft()
            if not queue:
                del self._waiting[nxt]
            if waiter.done():
                continue
            self._grant(nxt)
            waiter.set_result(None)


#: `Budget.held` with no key: every key's units together.
_EVERY_KEY = object()


class Budget:
    """Gates' kin for amounts rather than places: units (a request body's
    bytes) taken per key, at most `per_key` for one key and `total` for every
    key together, granted or refused at once, never waited for, and given
    back by their taker. The table holds the keys holding units now. Touched
    only on the event loop.

    The bodies under the common ceiling (main._BodyCap, per network): one
    sender's stalled uploads took the whole of a process-wide count and every
    other account's write answered 503 (the review of the audit of 27 Oct
    2026), as
    one account's took both large places before Gates had a per-key share."""

    def __init__(self, per_key: int, total: int) -> None:
        self.per_key = per_key
        self.total = total
        self._held: Dict[Any, int] = {}
        self._all = 0

    def held(self, key: Any = _EVERY_KEY) -> int:
        """What `key` holds now; every key's, with no key."""
        return self._all if key is _EVERY_KEY else self._held.get(key, 0)

    def take(self, key: Any, units: int) -> bool:
        """`units` more for `key`, or False and nothing taken."""
        mine = self._held.get(key, 0)
        if mine + units > self.per_key or self._all + units > self.total:
            return False
        if units:
            self._held[key] = mine + units
            self._all += units
        return True

    def give_back(self, key: Any, units: int) -> None:
        if not units:
            return
        left = self._held[key] - units
        if left:
            self._held[key] = left
        else:
            del self._held[key]
        self._all -= units


class Refused(Exception):
    """A place refused at once: its key holds as many as it may. A request
    answers it 429 with Retry-After (`retry_after`, seconds)."""

    def __init__(self, message: str, retry_after: int = BUSY_RETRY_SECONDS) -> None:
        super().__init__(message)
        self.retry_after = int(retry_after)


class Place:
    """One taken place. Held by its taker and by any work it started that
    outlives it (`hold`); given back when the last of them releases it."""

    __slots__ = ("_places", "key", "_users", "_lock")

    def __init__(self, places: "Places", key: Any) -> None:
        self._places, self.key = places, key
        self._users = 1
        self._lock = threading.Lock()

    def hold(self) -> None:
        with self._lock:
            self._users += 1

    def release(self) -> None:
        with self._lock:
            self._users -= 1
            last = self._users == 0
        if last:
            self._places._give_back(self.key)


class Places:
    """Places per key, taken from any thread and refused at once past
    `per_key` (`refusal(n)` is raised, n the places held): a caller already on
    a request's thread does not wait for one. Work started under a place
    (`held`) that outlives its caller holds the place on, so the place counts
    the resource, not the caller (`lookup`)."""

    def __init__(self, per_key: int, refusal: Optional[Callable[[int], Exception]] = None) -> None:
        self.per_key = per_key
        self._refusal = refusal or (lambda n: Refused(f"{n} of these are under way for this "
                                                      f"account: try again once one has ended"))
        self._lock = threading.Lock()
        self._held: Dict[Any, int] = {}

    def take(self, key: Any) -> Place:
        with self._lock:
            n = self._held.get(key, 0)
            if n >= self.per_key:
                raise self._refusal(n)
            self._held[key] = n + 1
        return Place(self, key)

    @contextlib.contextmanager
    def held(self, key: Any):
        """A place of `key` for the length of the block, or the refusal. The
        work in the block is `key`'s (`lookup` reads it)."""
        place = self.take(key)
        tokens = (_PLACE.set(place), _ACCOUNT.set(key))
        try:
            yield place
        finally:
            _ACCOUNT.reset(tokens[1])
            _PLACE.reset(tokens[0])
            place.release()

    def _give_back(self, key: Any) -> None:
        with self._lock:
            left = self._held.get(key, 1) - 1
            if left > 0:
                self._held[key] = left
            else:
                self._held.pop(key, None)


@contextlib.contextmanager
def for_account(account: Any):
    """The work in the block is `account`'s, for the bounds that need to know
    whose it is (`lookup`) and that no place names: a worker's attempt."""
    token = _ACCOUNT.set(account)
    try:
        yield
    finally:
        _ACCOUNT.reset(token)


_lookups = Places(LOOKUPS_PER_ACCOUNT, refusal=lambda n: Refused(
    f"{n} name lookups of this account are under way: try again once one has answered",
    retry_after=int(LOOKUP_SECONDS)))


def lookup(fn: Callable, *args, seconds: Optional[float] = None):
    """`fn(*args)`, a name lookup, in a thread of the lookups' pool, waited
    for at most `seconds` (TimeoutError past it); `Refused` while the
    account (the place this work holds, or `for_account`) has
    `LOOKUPS_PER_ACCOUNT` under way. A lookup whose caller stopped waiting
    goes on in its thread until the resolver gives up, and keeps both the
    account's lookup place and the place its caller held (`Places.held`)
    until then: the caller's thread is free, the resource is still counted
    (audit of 20 Oct 2026: one account's slow names held the request threads every
    account shares)."""
    place = _PLACE.get()
    account = _ACCOUNT.get()
    mine = _lookups.take(account)
    if place is not None:
        place.hold()

    def ended(_future=None) -> None:
        mine.release()
        if place is not None:
            place.release()

    try:
        future = pool("lookup", LOOKUP_THREADS).submit(
            contextvars.copy_context().run, fn, *args)
    except BaseException:
        ended()
        raise
    future.add_done_callback(ended)
    return future.result(timeout=max(0.0, LOOKUP_SECONDS if seconds is None else seconds))


# ── an account's places at the embedder ───────────────────────────────
class _Held:
    """An account's place at the embedder, held by the request in whose
    context it is (`waits_for_the_embedder`), until `release`: as soon as
    the request's vector has been computed (`at_the_embedder`), or at the
    end of its block if it never asked for one. Released once, from the
    request's thread or from the loop."""

    __slots__ = ("account", "released", "_gates", "_loop", "_lock")

    def __init__(self, account: str, gates: "Gates", loop: asyncio.AbstractEventLoop) -> None:
        self.account = account
        self.released = False
        self._gates, self._loop = gates, loop
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self.released:
                return
            self.released = True
        _soon_on_the_loop(self._loop, functools.partial(self._gates.give_back, self.account))


#: Every request's places at the embedder, `CALLS_PER_ACCOUNT` per account,
#: kept on the event loop (`Gates`): ONE count for both doors and for both
#: ways of taking one. The finding of 31 Oct 2026 (scripts/concurrent_agents.sh
#: at 400294b3: 30 agents of one account, 15 REST and 15 MCP, on two
#: processes): 549 searches were refused 503 `search_unavailable` and passed
#: only because the test client retried, up to 16 s; without retries about
#: half of them fail, and a real agent does not retry. Two reasons. A REST
#: search is a sync route, already on a request thread when it asks for its
#: vector, so the embedder refused an account's fifth at once rather than
#: let it wait there holding a thread (local_embedder._Turns, audit of
#: 19 Oct 2026); and the MCP's door let its fifth call wait for a place on
#: the loop, but its four then added up with REST's four at the embedder,
#: which refused the MCP's too. Now a read that needs a question's vector
#: (REST's POST /v1/memories/search and GET /v1/context, the screen's search,
#: MCP's korely_search and korely_get_context) waits for one of its account's
#: places here BEFORE it takes a thread for its search, as the MCP's door's
#: calls wait, once its own checks have passed (core/auth
#: .a_turn_for_its_question), and holds it until its vector has been
#: computed, never through the rest of its work (the review of c203d330: a
#: place held for the whole route bounded an account's reads in flight, not
#: its vectors): it waits its turn and holds nothing another account needs
#: while it waits (no thread, no connection). Any other request
#: whose thread asks for a vector (a write's) takes a place at once or goes
#: on without (`at_the_embedder`: the write is stored and the worker computes
#: its vector, as before), never ahead of a read waiting here. So what has
#: passed this gate is never refused by the embedder for its account's
#: places. No total: a key's place costs nothing until its request has a
#: thread, and the embedder serves the accounts in turn (_Turns), so a light
#: account's question goes before a heavy account's backlog.
_embedder = Gates(CALLS_PER_ACCOUNT)
_AT_THE_EMBEDDER: ContextVar[Optional[_Held]] = ContextVar("korely_embedder_place",
                                                           default=None)


def _whose(account: Any) -> str:
    """The key of an account's places: its id in the one spelling of a UUID,
    whichever door names it (REST's key row has a UUID, the MCP's verifier a
    string that could be written in capitals or with braces); a name that is
    not a UUID as it is."""
    try:
        return str(uuid.UUID(str(account)))
    except ValueError:
        return str(account)


@contextlib.asynccontextmanager
async def waits_for_the_embedder(account: Any):
    """One of `account`'s places at the embedder, waited for on the loop at
    most `WAIT_SECONDS`, and held by the request whose context this is until
    its thread's vector has been computed (core/local_embedder.embed, which
    does not count it again), or until the block ends. The block is told
    whether it got one: past the deadline it goes on without (False), and
    its question is then taken at once or refused at the embedder
    (`at_the_embedder`) as it was before 31 Oct 2026: a search answers 503
    `search_unavailable` with Retry-After, a context its facts with
    `degraded`. The wait holds no thread, so its caller must not hold a
    pooled connection either (core/auth.ready_to_wait)."""
    key = _whose(account)
    gates = _embedder
    got = False
    try:
        async with asyncio.timeout(WAIT_SECONDS):
            await gates.take(key)
        got = True
    except TimeoutError:
        pass
    if not got:
        yield False
        return
    held = _Held(key, gates, asyncio.get_running_loop())
    token = _AT_THE_EMBEDDER.set(held)
    try:
        yield True
    finally:
        held.release()
        with contextlib.suppress(ValueError):   # ended in another context
            _AT_THE_EMBEDDER.reset(token)


@contextlib.contextmanager
def at_the_embedder(account: Any):
    """What a vector asked on this thread does about its account's places
    (core/local_embedder.embed); the block is told whether it may go on. A
    request's caller that holds its account's place (`waits_for_the_embedder`)
    goes on, and gives the place back once the block ends; one that does not
    takes a place at once, on the request's loop, or is refused (False):
    never a wait on a request's thread. The worker's calls and the unbilled
    ones are nobody's request and go on uncounted."""
    if account is None or not on_a_request():
        yield True
        return
    key = _whose(account)
    held = _AT_THE_EMBEDDER.get()
    # One vector per read: the place a read waited for covers its first
    # vector only, and is given back as that vector ends. A second vector on
    # the same request falls to `take_now` below and may be refused. Today's
    # reads ask for one: a context's question once (services/context
    # ._query_vector, which hands it to the memory search as `query_vector`),
    # a search's in AgentMemoryEngine.search (services/memory_engine). A read
    # that comes to ask for two needs a place for each.
    if held is not None and held.account == key and not held.released:
        try:
            yield True
        finally:
            # Back as soon as the vector is: the rest of the read (its
            # search in the database, the block's layout) is not the
            # embedder's to bound (the review of c203d330).
            held.release()
        return
    loop = _LOOP.get()
    if not _on_the_loop(loop, functools.partial(_embedder.take_now, key)):
        yield False
        return
    try:
        yield True
    finally:
        _soon_on_the_loop(loop, functools.partial(_embedder.give_back, key))


async def on_a_request_thread(fn, *args):
    """`fn(*args)` on one of the request threads sync routes run on (AnyIO's
    pool, the one FastAPI gives a sync route), awaited: an async route's
    synchronous parts, before and after it waits on the loop for its turn at
    the embedder (core/auth.a_turn_for_its_question). The context is copied,
    as for a sync route; a cancelled caller waits for the thread to end, as
    a sync route's does."""
    import anyio

    return await anyio.to_thread.run_sync(functools.partial(fn, *args))


def _on_the_loop(loop: Optional[asyncio.AbstractEventLoop], fn: Callable[[], bool]) -> bool:
    """`fn()`, one step that never waits, run on `loop` from this thread, and
    its answer; False when there is no loop to run it on, or it did not run
    within `LOCK_WAIT_SECONDS` (it is then never run)."""
    if loop is None or loop.is_closed():
        return False
    try:
        if asyncio.get_running_loop() is loop:
            return fn()
    except RuntimeError:
        pass
    answer: concurrent.futures.Future = concurrent.futures.Future()

    def run() -> None:
        if answer.set_running_or_notify_cancel():
            try:
                answer.set_result(fn())
            except BaseException as e:  # noqa: BLE001 - handed to the caller
                answer.set_exception(e)
    try:
        loop.call_soon_threadsafe(run)
    except RuntimeError:            # the loop has closed meanwhile
        return False
    try:
        return answer.result(timeout=LOCK_WAIT_SECONDS)
    except concurrent.futures.TimeoutError:
        if answer.cancel():
            return False
        return answer.result()


def _soon_on_the_loop(loop: Optional[asyncio.AbstractEventLoop], fn: Callable[[], None]) -> None:
    """`fn()` on `loop`, not waited for; nothing once the loop has closed,
    when nothing is left to give a place to."""
    if loop is None or loop.is_closed():
        return
    try:
        if asyncio.get_running_loop() is loop:
            fn()
            return
    except RuntimeError:
        pass
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(fn)


# ── a process's lock ──────────────────────────────────────────────────────
@contextlib.contextmanager
def taken(lock):
    """`lock` for the block, which is told whether it got it: a request
    waits for it at most `LOCK_WAIT_SECONDS`, as for a row's lock, then goes
    on without (False); the worker waits as long as it takes. The audit of
    27 Oct 2026: an erasure's request waited with no end, holding its thread and
    its connection, for the lock the worker's export of every tenant held
    (services/export_md)."""
    got = lock.acquire(timeout=LOCK_WAIT_SECONDS) if on_a_request() else lock.acquire()
    try:
        yield got
    finally:
        if got:
            lock.release()


# ── the database's deadlines ──────────────────────────────────────────────
def engine_pool() -> dict:
    """The engine's pool arguments (db/base.get_engine)."""
    return {"pool_size": POOL_SIZE, "max_overflow": POOL_OVERFLOW,
            "pool_timeout": POOL_CHECKOUT_SECONDS}


def own_engine_pool() -> dict:
    """The own sessions' engine's pool arguments (db/base.get_own_engine)."""
    return {"pool_size": OWN_POOL_SIZE, "max_overflow": OWN_POOL_OVERFLOW,
            "pool_timeout": POOL_CHECKOUT_SECONDS}


def _set_deadlines(conn, lock_seconds: float, statement_seconds: float) -> None:
    conn.exec_driver_sql(
        f"SET LOCAL lock_timeout = {int(lock_seconds * 1000)}; "
        f"SET LOCAL statement_timeout = {int(statement_seconds * 1000)}")


def with_deadlines(engine) -> None:
    """Every transaction a request begins on `engine` has the request's
    deadlines (`LOCK_WAIT_SECONDS`, `STATEMENT_SECONDS`), an admission job's
    the shorter lock wait (`ADMISSION_LOCK_WAIT_SECONDS`), set for that
    transaction only (SET LOCAL): a connection the worker takes next from the
    pool has none. The worker's transactions keep waiting: two drain groups
    resolving the same relation wait for each other's commit by design
    (memory_core), and nothing of theirs holds a request's thread."""
    from sqlalchemy import event

    if engine.dialect.name != "postgresql":
        return

    @event.listens_for(engine, "begin")
    def _a_requests_deadlines(conn) -> None:
        if _ADMITTING.get():
            _set_deadlines(conn, ADMISSION_LOCK_WAIT_SECONDS, STATEMENT_SECONDS)
        elif on_a_request():
            _set_deadlines(conn, LOCK_WAIT_SECONDS, STATEMENT_SECONDS)


def request_deadlines(db) -> None:
    """The rest of this transaction has a request's deadlines
    (`LOCK_WAIT_SECONDS`, `STATEMENT_SECONDS`) wherever it runs, the worker
    included: a trail row written in clear waits that long for an open
    erasure of its end user, then takes the pseudonym (services/audit._settle,
    audit of 20 Oct 2026)."""
    if db.get_bind().dialect.name != "postgresql":
        return
    _set_deadlines(db.connection(), LOCK_WAIT_SECONDS, STATEMENT_SECONDS)


def longer_deadlines(db) -> None:
    """The rest of this transaction has an erasure's deadlines
    (`LONG_LOCK_WAIT_SECONDS`, `LONG_STATEMENT_SECONDS`): it waits for the
    writes under way into what it erases to commit, and it is its account's
    own request. Called by an erasure right before its first lock."""
    if db.get_bind().dialect.name != "postgresql":
        return
    _set_deadlines(db.connection(), LONG_LOCK_WAIT_SECONDS, LONG_STATEMENT_SECONDS)


def deadline_passed(exc: BaseException) -> bool:
    """Whether a database error is a deadline of this module given up on
    (a lock wait or a statement): the request is answered 503 `unavailable`,
    retry, never a 500."""
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    return code in _DEADLINE_CODES


def busy_refusal():
    """The 503 a request gets when one of its deadlines passed."""
    from fastapi import HTTPException

    return HTTPException(
        status_code=503,
        detail={"code": "unavailable",
                "message": "The database is busy with another change to the same records: "
                           "retry in a few seconds."},
        headers={"Retry-After": str(BUSY_RETRY_SECONDS)})


# ── a worker's rounds ─────────────────────────────────────────────────────
class Lanes:
    """A worker's threads, shared among accounts in time as well as in count.

    A round of a worker (the webhook deliveries, the facts drain's groups)
    used to start its jobs on a pool of its own and wait for the last of
    them: one account whose jobs ran long (a receiver that holds the
    connection to its deadline, a group of long memories) set the pace of
    every account's next round, and its jobs took every thread while the
    others' queued behind them (audit of 20 Oct 2026). Here a round starts each job in a thread of
    its own when a slot is free and its account is under its share
    (`share`: the slots divided among the accounts in play, this round's and
    the ones still running from earlier rounds; fair queueing, Demers,
    Keshav and Shenker, SIGCOMM 1989, as the claims are shared), collects
    what ends within `patience`, and hands back the jobs it never started,
    for their claim to be released. A job still running then goes on in its
    thread and writes its own outcome, and its account's share counts it
    until it ends, or until `give_up` seconds after it started (a thread
    cannot be stopped; one that hangs past that is left, as a pool's was,
    and logged).
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._cv = threading.Condition()
        # job id -> (account, give up at): the slots taken
        self._running: Dict[int, Tuple[Any, float]] = {}
        # job id -> account: the threads not ended, given up on or not
        self._alive: Dict[int, Any] = {}
        self._seq = 0

    def _sweep(self) -> None:
        now = time.monotonic()
        late = [j for j, (_a, until) in self._running.items() if until <= now]
        for j in late:
            del self._running[j]
        if late:
            logger.error("[%s] %d job(s) still running past their deadline: their slots are "
                         "given back, their threads left", self.name, len(late))

    def running(self) -> Dict[Any, int]:
        """{account: jobs running}, the ones of every round."""
        with self._cv:
            self._sweep()
            return self._counts()

    def _counts(self) -> Dict[Any, int]:
        out: Dict[Any, int] = {}
        for account, _until in self._running.values():
            out[account] = out.get(account, 0) + 1
        return out

    def share(self, slots: int, accounts: Iterable[Any], most: Optional[int] = None) -> int:
        """The slots one account may hold: `slots` divided among `accounts`
        and the accounts with jobs running, at most `most`, at least one."""
        with self._cv:
            self._sweep()
            n = len(set(accounts) | {a for a, _u in self._running.values()})
        s = -(-max(1, int(slots)) // max(1, n))
        if most is not None:
            s = min(s, most)
        return max(1, s)

    def free(self, slots: int) -> int:
        with self._cv:
            self._sweep()
            return max(0, int(slots) - len(self._running))

    def wait_for_room(self, slots: int, seconds: float) -> int:
        """Free slots, waited for at most `seconds` when there are none."""
        until = time.monotonic() + seconds
        with self._cv:
            while True:
                self._sweep()
                free = int(slots) - len(self._running)
                left = until - time.monotonic()
                if free > 0 or left <= 0:
                    return max(0, free)
                self._cv.wait(left)

    def wait_idle(self, seconds: float) -> bool:
        """Whether every job ended within `seconds`: the facts drain, resting
        on its threads while a person's next memories wait behind the group
        still running (services/facts/drain.process_pending_memories), and
        the tests that must see every outcome written."""
        until = time.monotonic() + seconds
        with self._cv:
            while self._alive:
                left = until - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
            return True

    def run(self, jobs: Sequence[Tuple[Any, Callable[[], Any]]], *, slots: int, share: int,
            patience: float, give_up: float) -> Tuple[Dict[int, Any], List[int]]:
        """Start `jobs`, [(account, fn)] in the order they should start, and
        collect them for at most `patience` seconds. Returns ({index: what fn
        returned, or the exception it raised} of the jobs that ended,
        [indexes of the jobs never started]); every other job is still
        running and writes its own outcome."""
        results: Dict[int, Any] = {}
        todo = list(range(len(jobs)))
        started = 0
        until = time.monotonic() + patience
        with self._cv:
            while True:
                self._sweep()
                counts = self._counts()
                total = len(self._running)
                for i in list(todo):
                    if total >= slots:
                        break
                    account = jobs[i][0]
                    if counts.get(account, 0) >= share:
                        continue
                    self._start(i, jobs[i], results, give_up)
                    counts[account] = counts.get(account, 0) + 1
                    total += 1
                    started += 1
                    todo.remove(i)
                if len(results) == started and not todo:
                    break
                left = until - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(left)
            return dict(results), todo

    def _start(self, index: int, job: Tuple[Any, Callable[[], Any]], results: dict,
               give_up: float) -> None:
        """Called under the condition."""
        account, fn = job
        jid = self._seq
        self._seq += 1
        self._running[jid] = (account, time.monotonic() + give_up)
        self._alive[jid] = account
        ctx = contextvars.copy_context()

        def run() -> None:
            try:
                out = ctx.run(_as_account, account, fn)
            except BaseException as e:  # noqa: BLE001 - the job's outcome, handed back
                out = e
            with self._cv:
                self._running.pop(jid, None)
                self._alive.pop(jid, None)
                results[index] = out
                self._cv.notify_all()

        threading.Thread(target=run, name=f"{self.name}-{jid}", daemon=True).start()


def _as_account(account: Any, fn: Callable[[], Any]) -> Any:
    with for_account(account):
        return fn()
