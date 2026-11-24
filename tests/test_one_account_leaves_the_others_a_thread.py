"""One account's MCP tool calls never keep another account out.

Audit of 19 Oct 2026: every MCP tool body, the MCP's key check, the per-call
counter, OAuth's provider and the pre-route key lookup waited on the loop's one
default pool (core/threads.off_the_loop), min(32, CPUs + 4) threads with one
FIFO queue, and nothing bounded how many tool calls one account had running. A
read's thread then waits in the local embedder's `_Turns`, one inference at a
time: one key's 40 concurrent korely_search calls took every thread, and
another account's key check waited behind all of them (measured 1.3 ms going to
1512 ms). `_Turns` is fair per account, but only to callers that already hold a
thread.

The real server, `call_tool`, verifier and pools here; the engine's search
replaced by a stand-in that blocks on an Event until the test lets it go, so
nothing depends on how long anything takes: before the fix the other
account's work could never start while the Event was unset.
"""
from __future__ import annotations

import asyncio
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from tests import cloud
import pytest

A = str(uuid.UUID(int=0xA))
B = str(uuid.UUID(int=0xB))
POOL = 5          # the default pool of a one-CPU host: min(32, 1 + 4)
BURST = 3 * POOL  # account A's concurrent calls, three times the pool
PROMPT = 2.0      # what "prompt" means here; before the fix: never


class _NoSession:
    def close(self):
        pass

    def rollback(self):
        pass

    def commit(self):
        pass


# The arguments every korely_search call here adds: none for the memories'
# path, and those of each path a part of the product adds to the search
# (core/extensions.Part.search), each test run on every one (the skeptic's
# finding of 2 Nov 2026: with a part's path switched off, these guards never
# ran it).
_KIND: dict = {}
_PATHS = cloud.of_the_parts("SEARCH_PATHS")


@pytest.fixture(params=["memories", *_PATHS])
def mcp(monkeypatch, request):
    from korely_agent.config import get_settings
    from korely_agent.mcp import server as mcp_server

    if not os.environ.get("DATABASE_URL"):
        monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/nothing")
    get_settings.cache_clear()
    monkeypatch.setattr(mcp_server, "get_sessionmaker", lambda: _NoSession)
    monkeypatch.setattr(mcp_server, "_audit", lambda *a, **k: None)
    # No database. A part's path: its own rows answer none (the engine's
    # search, faked below, is what is held); the question's one vector is the
    # fake engine's.
    _KIND.clear()
    if request.param in _PATHS:
        _KIND.update(_PATHS[request.param](monkeypatch))
    # Every key presented is account B's.
    monkeypatch.setattr(mcp_server, "_look_up_key",
                        lambda token: (B, None, ["memories:read"], "key-b", None))
    yield mcp_server
    get_settings.cache_clear()


class _Searches:
    """The engine's search: account A's block until `release` is set (an
    inference that never ends), account B's answer at once."""

    def __init__(self):
        self.release = threading.Event()
        self.running = 0
        self.most = 0
        self._lock = threading.Lock()

    def engine(self, outer):
        searches = self

        class _Engine:
            def __init__(self, db, uid, project, end_users=None):
                self.uid = str(uid)

            def question_vector(self, query, agent_id=None):
                return [0.0] * 768

            def search(self, query, **kw):
                if self.uid != A:
                    return []
                with searches._lock:
                    searches.running += 1
                    searches.most = max(searches.most, searches.running)
                try:
                    assert searches.release.wait(30), "the test never released account A"
                finally:
                    with searches._lock:
                        searches.running -= 1
                return []

        return _Engine


async def _with_a_burst_of_account_a(mcp, searches, then):
    """Account A's BURST korely_search calls in flight, as many of them
    running as the server lets run; then `then()`, then A let go."""
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=POOL))
    server = mcp._get_agent_mcp()

    async def a_call(i):
        mcp._bind(A, None, ["memories:read"], "key-a", None)
        return await server.call_tool("korely_search", {"query": f"q{i}", **_KIND})

    burst = [asyncio.create_task(a_call(i)) for i in range(BURST)]
    try:
        # Every thread A may take is taken: at least the per-account bound
        # is running (and, before the fix, the whole pool).
        for _ in range(500):
            if searches.running >= min(POOL, getattr(mcp, "MAX_RUNNING_CALLS_PER_TENANT", POOL)):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)   # time for one more to start, if it can
        return await then(server)
    finally:
        searches.release.set()
        results = await asyncio.gather(*burst, return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, f"account A's calls failed: {errors[:3]!r}"


def _prompt(awaitable, what):
    async def go():
        try:
            return await asyncio.wait_for(awaitable, PROMPT)
        except asyncio.TimeoutError:
            pytest.fail(f"{what} waited more than {PROMPT} s behind account A's tool calls")
    return go()


def test_another_accounts_key_check_is_prompt_while_one_account_fills_the_pool(mcp, monkeypatch):
    searches = _Searches()
    monkeypatch.setattr(mcp, "AgentMemoryEngine", searches.engine(mcp))

    async def check(_server):
        return await _prompt(mcp.KorelyAgentKeyVerifier().verify_token("kor_self_b"),
                             "account B's key check")

    token = asyncio.run(_with_a_burst_of_account_a(mcp, searches, check))
    assert token is not None and token.subject == B


def test_another_accounts_tool_call_runs_while_one_account_fills_the_pool(mcp, monkeypatch):
    searches = _Searches()
    monkeypatch.setattr(mcp, "AgentMemoryEngine", searches.engine(mcp))

    async def b_searches(server):
        async def b_call():
            mcp._bind(B, None, ["memories:read"], "key-b", None)
            return await server.call_tool("korely_search", {"query": "mine", **_KIND})
        return await _prompt(b_call(), "account B's korely_search")

    out = asyncio.run(_with_a_burst_of_account_a(mcp, searches, b_searches))
    assert "no matches." in str(out)


def test_one_account_runs_its_bound_at_once_and_the_rest_wait_their_turn(mcp, monkeypatch):
    # Waiting, not refused: every call of the burst is answered (the
    # gather in `_with_a_burst_of_account_a` fails on any error).
    searches = _Searches()
    monkeypatch.setattr(mcp, "AgentMemoryEngine", searches.engine(mcp))

    async def count(_server):
        return searches.running

    running = asyncio.run(_with_a_burst_of_account_a(mcp, searches, count))
    assert running < POOL, f"account A ran {running} tool calls at once, the whole pool"
    assert running == searches.most == mcp.MAX_RUNNING_CALLS_PER_TENANT
    assert not mcp._running_calls, "an account's gate outlived its calls"


def test_the_admission_threads_see_the_callers_context():
    # The tenant, project and network travel in ContextVars, set on the
    # loop; asyncio.to_thread copied them, and the admission pool must too.
    import contextvars

    from korely_agent.core import threads

    var = contextvars.ContextVar("probe", default=None)

    def read():
        return var.get(), threading.current_thread().name

    async def go():
        var.set("tenant-b")
        return await threads.admission_off_the_loop(read)

    value, thread = asyncio.run(go())
    assert value == "tenant-b"
    assert thread.startswith("admission")


def test_a_cancelled_call_keeps_its_place_until_its_body_ends(mcp, monkeypatch):
    """Audit of 20 Oct 2026: a cancelled call gave its place back at once while
    its body still held a thread, and the account's next call took it: five of
    its bodies in the pool, past its four. Four calls of A block, a fifth waits
    at the door; one of the four is cancelled: its body still runs, so the
    fifth must not start until the bodies are let go."""
    searches = _Searches()
    monkeypatch.setattr(mcp, "AgentMemoryEngine", searches.engine(mcp))

    async def go():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=POOL))
        server = mcp._get_agent_mcp()

        async def a_call(i):
            mcp._bind(A, None, ["memories:read"], "key-a", None)
            return await server.call_tool("korely_search", {"query": f"q{i}", **_KIND})

        calls = [asyncio.create_task(a_call(i)) for i in range(mcp.MAX_RUNNING_CALLS_PER_TENANT + 1)]
        for _ in range(500):
            if searches.running >= mcp.MAX_RUNNING_CALLS_PER_TENANT:
                break
            await asyncio.sleep(0.01)
        calls[0].cancel()
        await asyncio.sleep(0.3)
        running = searches.running
        searches.release.set()
        await asyncio.gather(*calls, return_exceptions=True)
        return running

    assert asyncio.run(go()) == mcp.MAX_RUNNING_CALLS_PER_TENANT
    assert searches.most == mcp.MAX_RUNNING_CALLS_PER_TENANT, \
        "a cancelled call's place went to the account's next call while its body ran"
    assert not mcp._running_calls, "an account's gate outlived its calls"


# ── several accounts at their bound (audit of 27 Oct 2026) ────────────────
C = str(uuid.UUID(int=0xC))


class _Held:
    """The engine's search: accounts A's and C's block until the test lets
    one of them go (`go`), account B's answer at once."""

    def __init__(self):
        self.permits = {A: threading.Semaphore(0), C: threading.Semaphore(0)}
        self.running = {A: 0, C: 0}
        self._lock = threading.Lock()

    def engine(self):
        held = self

        class _Engine:
            def __init__(self, db, uid, project, end_users=None):
                self.uid = str(uid)

            def question_vector(self, query, agent_id=None):
                return [0.0] * 768

            def search(self, query, **kw):
                if self.uid not in held.permits:
                    return []
                with held._lock:
                    held.running[self.uid] += 1
                try:
                    assert held.permits[self.uid].acquire(timeout=30), "never let go"
                finally:
                    with held._lock:
                        held.running[self.uid] -= 1
                return []

        return _Engine

    def go(self, account, n=1):
        for _ in range(n):
            self.permits[account].release()


def test_another_accounts_call_starts_within_one_body_while_two_accounts_are_at_their_bound(
        mcp, monkeypatch):
    """The per-account bounds summed past the pool: the door bounded each
    account to four running bodies and nothing bounded them together, on the
    loop's default pool (five threads on one CPU, one FIFO queue): accounts A
    and C at their four filled it, C's backlog queued in the pool, and account
    B's call waited behind all of it, whatever ended. Now the bodies have a
    pool of their own and the door a total, fair among accounts: once one body
    ends, B's call is the next to run."""
    held = _Held()
    monkeypatch.setattr(mcp, "AgentMemoryEngine", held.engine())
    per = mcp.MAX_RUNNING_CALLS_PER_TENANT
    backlog = per + 2

    async def go():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=POOL))
        server = mcp._get_agent_mcp()

        async def call(account, i):
            mcp._bind(account, None, ["memories:read"], f"key-{account}", None)
            return await server.call_tool("korely_search", {"query": f"q{i}", **_KIND})

        calls = [asyncio.create_task(call(acc, i)) for acc in (A, C) for i in range(backlog)]
        try:
            for _ in range(500):
                if held.running[A] + held.running[C] >= min(POOL, 2 * per) \
                        and held.running[A] >= min(per, POOL - 1):
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            b = asyncio.create_task(call(B, 0))
            await asyncio.sleep(0.1)
            held.go(A)           # one of A's bodies ends
            return await _prompt(b, "account B's korely_search, one body after")
        finally:
            held.go(A, backlog)
            held.go(C, backlog)
            results = await asyncio.gather(*calls, return_exceptions=True)
            errors = [r for r in results if isinstance(r, BaseException)]
            assert not errors, f"A's and C's calls failed: {errors[:3]!r}"

    out = asyncio.run(go())
    assert "no matches." in str(out)
    assert not mcp._running_calls, "an account's gate outlived its calls"


def test_a_freed_place_goes_to_the_account_with_the_fewest_running():
    """The door's total, alone: the freed place goes to the waiting account
    holding the fewest, not to the oldest waiter. Before: a FIFO semaphore,
    so account A's four waiters, queued first, all ran before B's one."""
    from korely_agent.core.threads import Gates

    gates = Gates(per_key=4, total=2)
    order = []

    async def go():
        release = {k: asyncio.Event() for k in ("x1", "x2", "a1", "a2", "a3", "a4", "b1")}

        async def hold(account, name):
            async with gates.place(account):
                order.append(name)
                await release[name].wait()

        tasks = [asyncio.create_task(hold("X", "x1")), asyncio.create_task(hold("X", "x2"))]
        await asyncio.sleep(0)
        tasks += [asyncio.create_task(hold("A", f"a{i}")) for i in range(1, 5)]
        await asyncio.sleep(0)
        tasks.append(asyncio.create_task(hold("B", "b1")))
        await asyncio.sleep(0.01)
        for name in ("x1", "x2", "a1", "a2", "a3", "a4", "b1"):
            release[name].set()
            await asyncio.sleep(0.01)
        await asyncio.gather(*tasks)

    asyncio.run(go())
    assert order[:4] == ["x1", "x2", "a1", "b1"], order
    assert len(gates) == 0 and not gates._held and not gates._waiting


def test_a_cancelled_waiter_gives_its_turn_on():
    """A caller cancelled while waiting for the total leaves the queue; one
    cancelled as its place was granted gives the place on."""
    from korely_agent.core.threads import Gates

    gates = Gates(per_key=4, total=1)

    async def go():
        free = asyncio.Event()

        async def hold(account):
            async with gates.place(account):
                await free.wait()

        first = asyncio.create_task(hold("X"))
        await asyncio.sleep(0)
        waiting = asyncio.create_task(hold("A"))
        later = asyncio.create_task(hold("B"))
        await asyncio.sleep(0.01)
        waiting.cancel()
        await asyncio.sleep(0.01)
        free.set()
        await asyncio.wait_for(asyncio.gather(first, later), 2)
        assert waiting.cancelled()

    asyncio.run(go())
    assert gates._in_total == 0 and not gates._held and not gates._waiting and len(gates) == 0
