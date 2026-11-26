"""Under one account's burst a search waits its turn instead of failing.

The finding of 31 Oct 2026 (scripts/concurrent_agents.sh at 400294b3): 30
agents of one account, 15 REST and 15 MCP, against two processes; 600 writes,
none lost or doubled, and 549 searches refused 503 `search_unavailable` that
passed only because the test client retried, up to 16 s. Without retries about
half the searches fail, and a real agent does not retry. A REST search is a
sync route, already on a request thread when it asks for its vector, so the
embedder refused an account's fifth at once (audit of 19 Oct 2026: to keep one
account from holding the threads every account shares), and the MCP's calls,
which waited for their places at the MCP's door, added up with REST's at the
embedder and were refused too.

Now a read that needs a question's vector waits for one of its account's
places on the event loop before it takes a thread for its search (core/auth
.a_turn_for_its_question, core/threads.waits_for_the_embedder), REST's and the
MCP's drawing on one count. Driven here through the real app, its routes, its
database and the MCP's door, with the model replaced by a fake that takes a
fixed time per vector (tests/test_local_embedder.py has the turns and the
places alone).
"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from korely_agent.core import local_embedder, threads

PLACES = threads.CALLS_PER_ACCOUNT


class _SlowModel:
    """The model's session: `delay` seconds per vector, one at a time (the
    turns see to that), and the most callers of one account at the embedder
    at once, the one running and those waiting."""

    def __init__(self, delay):
        self.delay = delay
        self.most = {}
        self._lock = threading.Lock()
        # When set to an Event: every vector waits for it (a stalled model).
        self.stalled = None

    def run(self, names, feed):
        import numpy as np

        if self.stalled is not None:
            assert self.stalled.wait(30), "the test never let the model go"
        time.sleep(self.delay)
        return [np.full((1, local_embedder.M.DIMENSIONS), 3.0, dtype=np.float32)]


class _Tokens:
    def encode(self, text):
        return [100 + (ord(c) % 1000) for c in text]


@pytest.fixture
def served(db, monkeypatch):
    """The app on the test database, the local embedder with a slow fake
    model, and what each account met at the embedder: the most of its
    callers there at once, and the vectors refused for want of a place."""
    from fastapi.testclient import TestClient

    from korely_agent.config import get_settings
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    get_settings.cache_clear()
    model = _SlowModel(0.05)
    monkeypatch.setattr(local_embedder, "_model", (model, _Tokens(), ["input_ids", "attention_mask"]))
    monkeypatch.setattr(local_embedder, "warm_up", lambda: None)
    local_embedder._last.update({"ok": None, "at": None, "error": None})

    met = {"refused": [], "most": {}}
    turns = local_embedder._Turns()
    real_turn = turns.turn
    lock = threading.Lock()

    def turn(question, account=None, request=False):
        with lock:
            here = turns._here(account) + 1
            met["most"][str(account)] = max(met["most"].get(str(account), 0), here)
        return real_turn(question, account, request)

    monkeypatch.setattr(turns, "turn", turn)
    monkeypatch.setattr(local_embedder, "_turns", turns)
    real_place = local_embedder.at_the_embedder

    class _Watched:
        def __init__(self, account):
            self.cm = real_place(account)
            self.account = account

        def __enter__(self):
            ok = self.cm.__enter__()
            if not ok:
                met["refused"].append(str(self.account))
            return ok

        def __exit__(self, *exc):
            return self.cm.__exit__(*exc)

    monkeypatch.setattr(local_embedder, "at_the_embedder", _Watched)
    # The places of this test alone: one given back from a thread after the
    # app's loop has stopped would otherwise be the next test's.
    monkeypatch.setattr(threads, "_embedder", threads.Gates(PLACES))
    with TestClient(create_app()) as c:
        yield c, model, met
    local_embedder._last.update({"ok": None, "at": None, "error": None})
    get_settings.cache_clear()


def _account(db, name):
    from korely_agent.db.models import Tenant
    from korely_agent.services import keys as key_service

    t = Tenant(id=uuid.uuid4(), name=name)
    db.add(t)
    db.commit()
    row, full = key_service.create_api_key(db, tenant_id=t.id, name=f"k-{uuid.uuid4().hex[:6]}",
                                           scopes=["memories:read", "memories:write"])
    return t, row, {"Authorization": f"Bearer {full}"}


def _at_once(n, fn):
    """`fn(i)` for i in range(n), all sent at once; their answers in order."""
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(fn, range(n)))


def test_one_accounts_burst_of_searches_is_all_answered(served, db):
    c, _model, met = served
    a, _row, head = _account(db, "A")
    burst = 4 * PLACES
    answers = _at_once(burst, lambda i: c.post("/v1/memories/search", headers=head,
                                               json={"query": f"where {i}?"}))
    assert [r.status_code for r in answers] == [200] * burst, \
        [r.text for r in answers if r.status_code != 200][:3]
    assert not met["refused"], f"{len(met['refused'])} vectors refused at the embedder"
    assert met["most"][str(a.id)] <= PLACES, met["most"]


def test_the_context_waits_its_turn_too(served, db):
    c, _model, met = served
    _a, _row, head = _account(db, "A")
    burst = 3 * PLACES
    answers = _at_once(burst, lambda i: c.get("/v1/context", headers=head,
                                              params={"query": f"what about {i}?"}))
    assert [r.status_code for r in answers] == [200] * burst
    assert not [r for r in answers if r.json()["degraded"]], "a context was built without its question"
    assert not met["refused"]


@pytest.mark.serial  # B's two requests timed under 1 s beside a burst of 4 * PLACES threads: under load from parallel neighbours the machine, not the turns, is what B waits for (finding of 25 Nov 2026)
def test_another_account_is_served_promptly_during_the_burst(served, db):
    """B's request that needs no vector, and B's search, while A's backlog
    is three times A's places deep: neither waits for A's backlog (a light
    account before a heavy account's backlog: the turns are fair among
    accounts, and A's waiting searches hold no thread)."""
    c, model, met = served
    model.delay = 0.15
    _a, _arow, a_head = _account(db, "A")
    _b, _brow, b_head = _account(db, "B")
    burst = 4 * PLACES          # some 2.4 s of A's vectors, one at a time
    pool = ThreadPoolExecutor(max_workers=burst)
    try:
        a_searches = [pool.submit(c.post, "/v1/memories/search", headers=a_head,
                                  json={"query": f"q{i}"}) for i in range(burst)]
        time.sleep(0.3)
        started = time.monotonic()
        plain = c.get("/v1/facts", headers=b_head)
        plain_took = time.monotonic() - started
        started = time.monotonic()
        search = c.post("/v1/memories/search", headers=b_head, json={"query": "mine"})
        search_took = time.monotonic() - started
        a_left = sum(1 for f in a_searches if not f.done())
        answers = [f.result(60) for f in a_searches]
    finally:
        pool.shutdown(wait=True)
    assert plain.status_code == 200 and plain_took < 1.0, plain_took
    assert search.status_code == 200 and search_took < 1.0, search_took
    assert a_left >= PLACES, f"B's search waited for A's backlog: {a_left} of A's left"
    assert [r.status_code for r in answers] == [200] * burst
    assert not met["refused"]


def test_a_waiting_search_holds_no_connection(served, db):
    """The searches past their account's places wait on the loop holding no
    pooled connection: their checks' session ends its transaction before the
    wait (core/auth.ready_to_wait). With the model stalled, the account's
    places stay with the searches at the embedder, and every other search of
    the burst waits for one: the pool's connections checked out then are at
    most the holders', whatever the database's speed."""
    from korely_agent.db.base import get_engine

    c, model, _met = served
    model.stalled = threading.Event()
    a, _row, head = _account(db, "A")
    key = threads._whose(a.id)
    burst = 4 * PLACES
    waiting = burst - PLACES
    pool = ThreadPoolExecutor(max_workers=burst)

    def waiters():
        gate = threads._embedder._gates.get(key)
        return 0 if gate is None else sum(1 for w in gate.waiters if not w.done())
    try:
        searches = [pool.submit(c.post, "/v1/memories/search", headers=head,
                                json={"query": f"q{i}"}) for i in range(burst)]
        until = time.monotonic() + 10
        while waiters() < waiting:
            assert time.monotonic() < until, f"only {waiters()} of {waiting} searches waited"
            time.sleep(0.01)
        held = []
        for _ in range(20):
            held.append(get_engine().pool.checkedout())
            time.sleep(0.01)
        assert waiters() == waiting, "a waiting search moved while the model was stalled"
        model.stalled.set()
        answers = [f.result(60) for f in searches]
    finally:
        model.stalled.set()
        pool.shutdown(wait=True)
    assert [r.status_code for r in answers] == [200] * burst
    assert max(held) <= PLACES, \
        f"{max(held)} connections checked out while {waiting} searches waited for a place"


def test_mcp_and_rest_of_one_account_together_never_meet_a_refusal(served, db):
    """The finding's other half: the MCP's door let its calls wait for its
    own four places, and REST's four added up with them at the embedder,
    which refused the MCP's searches too. Both now draw on one count."""
    from korely_agent.mcp import server as mcp_server

    c, _model, met = served
    a, row, head = _account(db, "A")
    mcp = mcp_server._get_agent_mcp()

    async def an_mcp_search(i):
        threads.serving_a_request()   # as the HTTP door's middleware does
        mcp_server._bind(str(a.id), None, ["memories:read"], str(row.id), None)
        return await mcp.call_tool("korely_search", {"query": f"mcp {i}"})

    async def an_mcp_context(i):
        threads.serving_a_request()
        mcp_server._bind(str(a.id), None, ["memories:read"], str(row.id), None)
        return await mcp.call_tool("korely_get_context", {"query": f"mcp context {i}"})

    n = 3 * PLACES

    def one(i):
        kind = i % 4
        if kind == 0:
            return c.post("/v1/memories/search", headers=head, json={"query": f"rest {i}"})
        if kind == 1:
            return c.get("/v1/context", headers=head, params={"query": f"rest {i}"})
        if kind == 2:
            return c.portal.call(an_mcp_search, i)
        return c.portal.call(an_mcp_context, i)

    answers = _at_once(4 * n, one)   # raises on an MCP error (ToolError)
    rest = [r for r in answers if hasattr(r, "status_code")]
    assert [r.status_code for r in rest] == [200] * len(rest)
    assert not [r for r in rest if "degraded" in r.json() and r.json()["degraded"]]
    assert len(rest) == 2 * n
    assert not met["refused"], f"{len(met['refused'])} vectors refused at the embedder"
    assert met["most"][str(a.id)] <= PLACES, met["most"]


def test_past_the_wait_a_search_is_503_with_retry_after(served, db, monkeypatch):
    """A search that waited WAIT_SECONDS for a place goes on without one, and
    its question is refused at the embedder as before: 503
    `search_unavailable`, now with Retry-After; the MCP's says
    `search_unavailable`; a context answers its facts, `degraded`."""
    from mcp.server.fastmcp.exceptions import ToolError

    from korely_agent.mcp import server as mcp_server

    c, _model, _met = served
    a, row, head = _account(db, "A")
    monkeypatch.setattr(threads, "WAIT_SECONDS", 0.3)
    monkeypatch.setattr(threads, "_embedder", threads.Gates(0))   # no place to be had
    started = time.monotonic()
    r = c.post("/v1/memories/search", headers=head, json={"query": "where?"})
    assert time.monotonic() - started >= 0.3
    assert (r.status_code, r.json()["code"]) == (503, "search_unavailable"), r.text
    assert r.headers["Retry-After"] == str(threads.BUSY_RETRY_SECONDS)
    r = c.get("/v1/context", headers=head, params={"query": "where?"})
    assert r.status_code == 200 and r.json()["degraded_parts"] == ["memories"], r.text

    async def an_mcp_search():
        threads.serving_a_request()
        mcp_server._bind(str(a.id), None, ["memories:read"], str(row.id), None)
        return await mcp_server._get_agent_mcp().call_tool("korely_search", {"query": "where?"})

    with pytest.raises(ToolError, match="search_unavailable"):
        c.portal.call(an_mcp_search)


def test_a_write_with_no_place_is_stored_and_its_vector_left_to_the_worker(served, db,
                                                                           monkeypatch):
    """Writes keep degrading as before: a write's vector, already on its
    request's thread, takes a place at once or goes without, and the memory
    is stored (201) with its vector pending for the worker."""
    from korely_agent.db.models import AgentMemory

    c, _model, met = served
    _a, _row, head = _account(db, "A")
    monkeypatch.setattr(threads, "_embedder", threads.Gates(0))
    r = c.post("/v1/memories", headers=head, json={"content": "I moved to Lisbon.", "user_id": "u"})
    assert r.status_code == 201, r.text
    db.expire_all()
    m = db.query(AgentMemory).one()
    assert m.embedding is None and m.embedding_status == "pending"
    assert met["refused"], "the write's vector was not asked for"


def test_the_workers_vectors_are_not_counted(served, db, monkeypatch):
    """With every place of the account taken, the worker's vectors drain
    computes the stored memory's vector: the worker holds no request's thread
    and is never refused."""
    from korely_agent.db.models import AgentMemory
    from korely_agent.services import vectors

    c, _model, met = served
    a, _row, head = _account(db, "A")
    monkeypatch.setattr(threads, "_embedder", threads.Gates(0))
    assert c.post("/v1/memories", headers=head,
                  json={"content": "I moved to Lisbon.", "user_id": "u"}).status_code == 201
    refused_by_requests = len(met["refused"])
    # Due now, not at the retry its write scheduled.
    db.query(AgentMemory).update({"embedding_next_at": None})
    db.commit()
    assert vectors.process_pending_vectors(db).get("processed") == 1
    db.expire_all()
    m = db.query(AgentMemory).one()
    assert m.embedding is not None, "the worker's vector was refused"
    assert len(met["refused"]) == refused_by_requests


# ── the review of c203d330 ────────────────────────────────────────────────

@pytest.mark.serial  # two reads timed against one (0.93 s against 0.58 s under 4 processes, release of 1.0.26): no parallel neighbours
def test_the_place_is_held_for_the_vector_not_for_the_whole_read(served, db, monkeypatch):
    """The place is given back once the read's vector has been computed: an
    account's reads in flight are not bounded by its places at the embedder,
    only its vectors are. One place, two searches whose database side takes
    0.5 s each: they overlap, where a place held through the route made them
    one after the other (1.14 s measured on c203d330)."""
    from korely_agent.services import memory_engine

    c, _model, met = served
    _a, _row, head = _account(db, "A")
    monkeypatch.setattr(threads, "_embedder", threads.Gates(1))
    real_search = memory_engine.AgentMemoryEngine.search

    def slow_search(self, *a, **kw):
        out = real_search(self, *a, **kw)
        time.sleep(0.5)          # the database side of the search, after the vector
        return out
    monkeypatch.setattr(memory_engine.AgentMemoryEngine, "search", slow_search)
    # One search alone, then two at once: two that held the place through
    # their database side would take twice one; two that overlap, about one.
    # Against the measured one, not a fixed bound, so a slow machine slows
    # both sides alike.
    started = time.monotonic()
    assert c.post("/v1/memories/search", headers=head, json={"query": "alone"}).status_code == 200
    one = time.monotonic() - started
    started = time.monotonic()
    answers = _at_once(2, lambda i: c.post("/v1/memories/search", headers=head,
                                           json={"query": f"q{i}"}))
    took = time.monotonic() - started
    assert [r.status_code for r in answers] == [200, 200]
    assert took < 1.5 * one, \
        f"the place was held through the database search: {took:.2f} s for two, {one:.2f} s for one"
    assert not met["refused"]
    until = time.monotonic() + 2     # given back from a thread, on the loop a moment later
    while len(threads._embedder) and time.monotonic() < until:
        time.sleep(0.01)
    assert len(threads._embedder) == 0, "a place outlived its read"


#: The wait a refused read would sit through if it waited, and the most a
#: refusal that does not wait takes here: far apart, so a slow first request
#: of a loaded suite (0.46 s measured) is not read as a wait.
_A_WAIT, _NO_WAIT = 3.0, 1.5


def _no_place_for_anyone(monkeypatch):
    monkeypatch.setattr(threads, "WAIT_SECONDS", _A_WAIT)
    monkeypatch.setattr(threads, "_embedder", threads.Gates(0))


@pytest.mark.parametrize("ask", [
    ("post", "/v1/memories/search", {"json": {"query": 123, "limit": "abc"}}),
    ("post", "/v1/memories/search", {"json": {"query": "   "}}),
    ("post", "/v1/memories/search", {"json": {"query": "hi", "user_id": ""}}),
    ("get", "/v1/context", {"params": {"query": "", "token_budget": "x"}}),
    ("get", "/v1/context", {"params": {"query": "hi", "token_budget": 1}}),
    ("get", "/v1/context", {"params": {"query": "   "}}),
    ("get", "/v1/context", {"params": {"query": "hi", "known_as_of": "yesterday"}}),
], ids=["search-types", "search-blank", "search-id", "context-types", "context-budget",
        "context-blank", "context-moment"])
def test_a_malformed_read_is_refused_before_it_waits(served, db, monkeypatch, ask):
    """A read the route refuses never waits for a place it does not need:
    FastAPI checks an async route's body and query before calling it, and
    the route's own checks run before its wait."""
    c, _model, _met = served
    _a, _row, head = _account(db, "A")
    _no_place_for_anyone(monkeypatch)
    method, path, kw = ask
    started = time.monotonic()
    r = getattr(c, method)(path, headers=head, **kw)
    took = time.monotonic() - started
    assert r.status_code == 422, r.text
    assert took < _NO_WAIT, f"a malformed read waited {took:.2f} s for a place before its 422"


def test_a_confined_key_naming_another_end_user_is_refused_before_it_waits(served, db,
                                                                           monkeypatch):
    from korely_agent.db.models import Tenant
    from korely_agent.services import keys as key_service

    c, _model, _met = served
    t = Tenant(id=uuid.uuid4(), name="C")
    db.add(t)
    db.commit()
    _row, full = key_service.create_api_key(db, tenant_id=t.id, name="confined",
                                            scopes=["memories:read"], end_users=["alice"])
    head = {"Authorization": f"Bearer {full}"}
    _no_place_for_anyone(monkeypatch)
    for method, path, kw in (("post", "/v1/memories/search", {"json": {"query": "hi", "user_id": "bob"}}),
                             ("get", "/v1/context", {"params": {"query": "hi", "user_id": "bob"}})):
        started = time.monotonic()
        r = getattr(c, method)(path, headers=head, **kw)
        took = time.monotonic() - started
        assert r.status_code == 403, r.text
        assert took < _NO_WAIT, f"an out-of-scope read waited {took:.2f} s for a place before its 403"


def test_a_key_without_the_scope_is_refused_before_it_waits(served, db, monkeypatch):
    from korely_agent.db.models import Tenant
    from korely_agent.services import keys as key_service

    c, _model, _met = served
    t = Tenant(id=uuid.uuid4(), name="W")
    db.add(t)
    db.commit()
    _row, full = key_service.create_api_key(db, tenant_id=t.id, name="w", scopes=["memories:write"])
    _no_place_for_anyone(monkeypatch)
    started = time.monotonic()
    r = c.post("/v1/memories/search", headers={"Authorization": f"Bearer {full}"},
               json={"query": "hi"})
    assert r.status_code == 403, r.text
    assert time.monotonic() - started < _NO_WAIT


def test_the_clouds_monthly_refusal_comes_before_the_wait(served, db, monkeypatch):
    """The month's count (count_the_read, the Cloud's 429 past its plan) is
    made before the read waits: a refused read never takes a place."""
    from fastapi import HTTPException

    from korely_agent.core.extensions import hooks

    c, _model, _met = served
    _a, _row, head = _account(db, "A")
    _no_place_for_anyone(monkeypatch)

    def past_the_month(db, tenant_id):
        raise HTTPException(429, {"code": "quota_exceeded", "message": "past the month"})
    monkeypatch.setattr(hooks, "before_query", past_the_month)
    for method, path, kw in (("post", "/v1/memories/search", {"json": {"query": "hi"}}),
                             ("get", "/v1/context", {"params": {"query": "hi"}})):
        started = time.monotonic()
        r = getattr(c, method)(path, headers=head, **kw)
        assert r.status_code == 429, r.text
        assert time.monotonic() - started < _NO_WAIT


def test_mcp_calls_waiting_for_the_door_hold_no_place_at_the_embedder(served, db, monkeypatch):
    """The review of c203d330: an MCP read took its place at the embedder and
    then waited for the door (its bodies' total, every account's, no
    deadline); with the door full of other accounts' bodies, the account's
    MCP reads held all its places and its REST searches waited 20 s and
    answered 503 with the embedder idle. Now a call waits for the door
    first, holding nothing at the embedder."""
    from korely_agent.mcp import server as mcp_server

    c, _model, met = served
    a, row, head = _account(db, "A")
    # The door full: every call waits for its total.
    monkeypatch.setattr(mcp_server, "_running_calls", threads.Gates(PLACES, total=0))
    monkeypatch.setattr(threads, "WAIT_SECONDS", 2.0)

    async def an_mcp_search(i):
        threads.serving_a_request()
        mcp_server._bind(str(a.id), None, ["memories:read"], str(row.id), None)
        return await mcp_server._get_agent_mcp().call_tool("korely_search", {"query": f"m{i}"})

    waiting = [c.portal.start_task_soon(an_mcp_search, i) for i in range(PLACES)]
    try:
        time.sleep(0.3)
        assert not any(f.done() for f in waiting), "the door let a call in"
        started = time.monotonic()
        r = c.post("/v1/memories/search", headers=head, json={"query": "rest"})
        took = time.monotonic() - started
        assert r.status_code == 200, r.text
        assert took < 1.0, f"REST waited {took:.2f} s behind MCP calls stuck at the door"
    finally:
        for f in waiting:
            f.cancel()
    assert not met["refused"]


def test_with_google_vectors_a_read_takes_no_place(monkeypatch):
    """EMBEDDING_PROVIDER=gemini: the places are the local embedder's, and a
    Google vector, a read's or a write's, takes none."""
    import asyncio
    from types import SimpleNamespace

    from korely_agent.core import auth

    monkeypatch.setattr(threads, "_embedder", threads.Gates(0))
    monkeypatch.setattr(threads, "WAIT_SECONDS", 5.0)

    async def go(provider):
        monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(embedding_provider=provider))
        started = time.monotonic()
        async with auth.a_turn_for_its_question(uuid.uuid4()) as got:
            return got, time.monotonic() - started

    got, took = asyncio.run(go("gemini"))
    assert got is True and took < 0.1
    monkeypatch.setattr(threads, "WAIT_SECONDS", 0.2)
    got, took = asyncio.run(go("local"))
    assert got is False and took >= 0.2


def test_one_account_is_one_key_however_a_door_spells_it():
    a = uuid.uuid4()
    spellings = {threads._whose(a), threads._whose(str(a)), threads._whose(str(a).upper()),
                 threads._whose("{" + str(a) + "}"), threads._whose(a.hex)}
    assert spellings == {str(a)}
    assert threads._whose("not-a-uuid") == "not-a-uuid"
