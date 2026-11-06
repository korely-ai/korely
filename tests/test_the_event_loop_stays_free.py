"""Nothing that waits on the network or the database runs on the event loop.

The image starts ONE uvicorn worker (the CMD in Dockerfile), so one event loop
serves every request: REST, MCP, the screen, and `/health`, which Docker asks
every thirty seconds with a five second timeout. A synchronous call made
inside an `async def` holds that loop for as long as the call takes, and every
other request waits behind it.

Two places did exactly that:

- The four MCP tools are `async def`, and FastMCP awaits them on the loop. Their
  bodies were synchronous from the first line: an embedding request to the
  model provider on every korely_search, korely_get_context and korely_add, and
  with WRITE_ASYNC=false the whole fact extraction inline. One agent
  writing a memory over MCP froze the install for the length of a model call,
  and the verifier in front of them did its database lookup on the loop too.
- The REST authentication dependencies (`verify_api_key`, `require_scopes`,
  `enforce_rate_limit`, `admin_principal`, `enforce_admin_rate_limit`) were
  `async def` around synchronous SQLAlchemy. FastAPI runs a plain `def`
  dependency in its threadpool and an `async def` one on the loop, so every
  authenticated request did its key lookup, its `last_used_at` write and its
  rate-limit upsert with the loop held. A slow query, a lock wait or an
  exhausted connection pool (whose release needs the loop to run the other
  requests' teardown) stopped the server outright.

How it is measured: a ticker coroutine wakes every 10 ms and records the
longest gap between two of its turns, while the code under test runs with its
blocking call replaced by a `time.sleep` of known length. Blocking on the loop
shows up as a gap of that length; blocking in a thread does not show up at all.
The stand-ins mean no database, no network and no model are needed, so this
runs in `make test`.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest

STALL = 0.6   # how long each stand-in blocks
FREE = 0.3    # the longest the loop may go without a turn


async def _longest_gap(awaitable):
    """Await `awaitable` while a ticker measures the loop; (result, longest gap)."""
    loop = asyncio.get_running_loop()
    gaps: list[float] = []
    done = asyncio.Event()

    async def ticker():
        last = loop.time()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = loop.time()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)  # the ticker is running before the call starts
    try:
        result = await awaitable
    finally:
        done.set()
        await task
    return result, max(gaps)


def _blocking(result):
    def stand_in(*_a, **_k):
        time.sleep(STALL)
        return result
    return stand_in


@pytest.fixture(autouse=True)
def _a_configuration(monkeypatch):
    """Building the MCP server reads the settings, and the settings want a
    database URL. `make test` has none; nothing here connects to it."""
    from korely_agent.config import get_settings

    if not os.environ.get("DATABASE_URL"):
        monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/nothing")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _NoSession:
    """What the tools open and close; nothing here reaches a database."""

    def close(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def mcp(monkeypatch):
    from korely_agent.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "get_sessionmaker", lambda: _NoSession)
    monkeypatch.setattr(mcp_server, "log_v1_request", lambda *a, **k: None)
    return mcp_server


def _tool(mcp_server, name):
    return mcp_server._get_agent_mcp()._tool_manager._tools[name].fn


def _as_a_tenant(mcp_server, awaitable_factory):
    """Run a tool the way the transport does: after verification set the tenant."""
    async def go():
        mcp_server._agent_user_id.set(str(uuid.uuid4()))
        mcp_server._agent_project.set(None)
        return await _longest_gap(awaitable_factory())
    return asyncio.run(go())


class TestAFailedReadIsNotAnEmptyMemory:
    """korely_get_context said "nothing remembered yet" when the retrieval
    had failed and the block came back empty; REST says `degraded` (the audit
    of 2026-10-06, row 6)."""

    def _context(self, mcp, monkeypatch, out):
        monkeypatch.setattr(mcp, "assemble_context", lambda *a, **k: out)
        return _as_a_tenant(mcp, lambda: _tool(mcp, "korely_get_context")(query="plan",
                                                                          user_id="u1"))[0]

    def test_degraded_and_empty_says_so(self, mcp, monkeypatch):
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as e:     # isError, since 2026-10-06
            self._context(mcp, monkeypatch, {"context": "", "degraded": True,
                                             "degraded_parts": ["memories"], "sources": []})
        assert str(e.value).startswith("error:") and "not an empty result" in str(e.value)

    def test_empty_and_healthy_is_nothing_remembered(self, mcp, monkeypatch):
        said = self._context(mcp, monkeypatch, {"context": "", "degraded": False, "sources": []})
        assert said == "(nothing remembered yet for this query)"

    def test_a_block_is_returned_as_it_is(self, mcp, monkeypatch):
        said = self._context(mcp, monkeypatch, {"context": "- the user is on Basic\n\nNOTE: partial",
                                                "degraded": True, "sources": []})
        assert said == "- the user is on Basic\n\nNOTE: partial"


class TestTheMcpTools:
    def test_korely_search_embeds_off_the_loop(self, mcp, monkeypatch):
        monkeypatch.setattr(mcp.AgentMemoryEngine, "search", _blocking([]))
        out, gap = _as_a_tenant(mcp, lambda: _tool(mcp, "korely_search")(query="plan", user_id="u1"))
        assert out == "no matches."
        assert gap < FREE, f"korely_search held the event loop for {gap:.2f}s"

    def test_korely_get_context_embeds_off_the_loop(self, mcp, monkeypatch):
        monkeypatch.setattr(mcp, "assemble_context", _blocking({"context": "- the user is on Basic"}))
        out, gap = _as_a_tenant(mcp, lambda: _tool(mcp, "korely_get_context")(query="plan", user_id="u1"))
        assert out == "- the user is on Basic"
        assert gap < FREE, f"korely_get_context held the event loop for {gap:.2f}s"

    def test_korely_add_writes_off_the_loop(self, mcp, monkeypatch):
        written = SimpleNamespace(id=uuid.uuid4(), end_user_id="u1", agent_id=None)
        monkeypatch.setattr(mcp.AgentMemoryEngine, "add", _blocking(written))
        monkeypatch.setattr("korely_agent.services.webhooks.emit_event", lambda *a, **k: None)
        out, gap = _as_a_tenant(mcp, lambda: _tool(mcp, "korely_add")(content="Moved to Basic.", user_id="u1"))
        assert out.startswith("Stored mem_")
        assert gap < FREE, f"korely_add held the event loop for {gap:.2f}s"

    def test_korely_add_extracts_off_the_loop_when_writes_are_synchronous(self, mcp, monkeypatch):
        # WRITE_ASYNC=false: the extraction, a model call of several
        # seconds, runs inside the tool.
        # Done, as a real extraction leaves it (facts.extract_inline reads it).
        written = SimpleNamespace(id=uuid.uuid4(), end_user_id="u1", agent_id=None,
                                  graph_sync_status="done")
        monkeypatch.setattr(mcp.AgentMemoryEngine, "add", lambda *a, **k: written)
        monkeypatch.setattr("korely_agent.services.webhooks.emit_event", lambda *a, **k: None)
        # The inline extraction the tool calls (facts.extract_inline, which
        # takes the write's claim first), as a call of several seconds.
        monkeypatch.setattr(mcp.agent_facts, "extract_inline", _blocking([]))
        # The real settings, read from the environment: the MCP server is
        # built afresh for every test (conftest.py) and reads them as it is.
        from korely_agent.config import get_settings

        monkeypatch.setenv("WRITE_ASYNC", "false")
        get_settings.cache_clear()
        out, gap = _as_a_tenant(mcp, lambda: _tool(mcp, "korely_add")(content="Moved to Basic.", user_id="u1"))
        assert out.startswith("Stored mem_")
        assert gap < FREE, f"the inline extraction held the event loop for {gap:.2f}s"

    def test_korely_get_facts_reads_off_the_loop(self, mcp, monkeypatch):
        monkeypatch.setattr(mcp.agent_facts, "retrieve_agent_facts", _blocking(([], 0)))
        out, gap = _as_a_tenant(mcp, lambda: _tool(mcp, "korely_get_facts")(user_id="u1"))
        assert out == "no facts."
        assert gap < FREE, f"korely_get_facts held the event loop for {gap:.2f}s"

    def test_the_tools_still_see_the_tenant_the_verifier_set(self, mcp, monkeypatch):
        # Off the loop means another thread, and the tenant travels in a
        # ContextVar. asyncio.to_thread copies the context; this pins that.
        seen = []
        monkeypatch.setattr(mcp.agent_facts, "retrieve_agent_facts",
                            lambda db, uid, **k: (seen.append(uid), ([], 0))[1])

        async def go():
            tenant = uuid.uuid4()
            mcp._agent_user_id.set(str(tenant))
            mcp._agent_project.set(None)
            await _tool(mcp, "korely_get_facts")(user_id="u1")
            return tenant

        assert seen == [asyncio.run(go())]

    def test_the_verifier_looks_the_key_up_off_the_loop(self, mcp, monkeypatch):
        monkeypatch.setattr(mcp.api_key_service, "verify_api_key", _blocking(None))

        async def go():
            return await _longest_gap(mcp.KorelyAgentKeyVerifier().verify_token("kor_self_nope"))

        token, gap = asyncio.run(go())
        assert token is None
        assert gap < FREE, f"the MCP key lookup held the event loop for {gap:.2f}s"


class TestTheRestAuthentication:
    """The real dependencies from core/auth.py, in front of two trivial routes."""

    @pytest.fixture
    def app(self, monkeypatch):
        from fastapi import Depends, FastAPI

        from korely_agent.core import auth
        from korely_agent.db import get_db
        from korely_agent.services.rate_limit import RateInfo

        tenant = uuid.uuid4()
        monkeypatch.setattr(auth.key_service, "verify_api_key", _blocking(
            SimpleNamespace(tenant_id=tenant, scopes=["memories:read", "admin"])))
        monkeypatch.setattr(auth.operator_service, "resolve", _blocking(SimpleNamespace(tenant_id=tenant)))
        monkeypatch.setattr(auth, "check_and_increment", _blocking(RateInfo(limit=0, remaining=0, reset_in=0)))
        monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(trust_auth_header=None))

        app = FastAPI()

        @app.get("/v1/probe", dependencies=[Depends(auth.enforce_rate_limit)])
        def v1(key=Depends(auth.require_scopes("memories:read"))):
            return {"tenant": str(key.tenant_id)}

        @app.get("/admin/probe", dependencies=[Depends(auth.enforce_admin_rate_limit)])
        def admin(principal=Depends(auth.admin_principal)):
            return {"tenant": str(principal.tenant_id)}

        app.dependency_overrides[get_db] = lambda: None
        return app

    @pytest.mark.parametrize("path, token", [
        ("/v1/probe", "kor_self_agent"),        # verify_api_key, require_scopes, enforce_rate_limit
        ("/admin/probe", "kor_sess_person"),    # admin_principal (session), enforce_admin_rate_limit
        ("/admin/probe", "kor_self_admin"),     # admin_principal (admin key)
    ])
    def test_authenticating_does_not_hold_the_loop(self, app, path, token):
        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://install") as client:
                return await _longest_gap(client.get(path, headers={"Authorization": f"Bearer {token}"}))

        response, gap = asyncio.run(go())
        assert response.status_code == 200, response.text
        assert gap < FREE, f"authenticating {path} held the event loop for {gap:.2f}s"
