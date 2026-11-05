"""The second door.

MCP is a second way into the same store, with the same keys. REST isolation has
a test as of today; this did not, and the product's whole proposition is that a
tenant cannot reach another tenant's data.

How the identity travels (since 2026-10-06): the token verifier checks the key
and puts the tenant, project, scopes and key id in the request's AccessToken;
every tools/call goes through `_ScopedFastMCP.call_tool`, whose
`_bind_the_caller` reads them from the token of that call's own HTTP request
into the context variables the tools read; and the session manager lets a
session be used only by the key that opened it (`client_id` is the key). A
call with no HTTP request behind it (these in-process tests, a stdio
transport) keeps what the verifier set in the context variables.

So there are two kinds of test here. `TestThroughTheHttpDoor` is the way
production takes: two tenants' keys over the mounted /mcp/ transport (audit of
17 Oct 2026: until then two tenants were tried only in-process, and the HTTP
path only with two keys of one tenant, tests/test_mcp_identity_per_request.py).
The others call the tool table directly and push on the context variables
themselves: a tool called with no verification in front of it, a tool called
after someone else's verification, two tenants interleaved.
"""
import asyncio
import uuid

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from tests.conftest import seeded_project_id
from tests import test_mcp_identity_per_request as _over_http
from tests.test_mcp_identity_per_request import _call, _open, _payload, _post
from korely_agent.db.models import Tenant
from korely_agent.mcp import server as mcp_server
from korely_agent.services import keys as key_service
from korely_agent.services.facts.service import write_fact_direct

# The real app over HTTP, the fixture of the per-request identity tests.
client = _over_http.client



@pytest.fixture
def two_tenants(db):
    a = Tenant(id=uuid.uuid4(), name="Alpha")
    b = Tenant(id=uuid.uuid4(), name="Beta")
    db.add_all([a, b])
    db.commit()
    _r, ka = key_service.create_api_key(db, tenant_id=a.id, name="a")
    _r, kb = key_service.create_api_key(db, tenant_id=b.id, name="b")

    # Facts rather than memories, and read back through korely_get_facts, which
    # is deterministic SQL. korely_get_context goes through semantic search,
    # which needs an embedding provider, so a suite without a key would be
    # measuring the absence of a key instead of the isolation.
    #
    # The markers are nonsense words on purpose. A first version of this file
    # looked for "red" and found it inside "nothing remembe(red) yet", and
    # reported a cross-tenant leak that did not exist.
    # In the project the key was born in. Writing them with no project would
    # model an install that can no longer exist: keys are born bound to the
    # account's default project, and a fact with no project would be seen by
    # nobody.
    write_fact_direct(db, a.id, subject="shared-name", predicate="keeps",
                      obj="ZZALPHAMARK", end_user_id="shared-name",
                      project_id=seeded_project_id(db, a.id))
    write_fact_direct(db, b.id, subject="shared-name", predicate="keeps",
                      obj="ZZBETAMARK", end_user_id="shared-name",
                      project_id=seeded_project_id(db, b.id))
    db.commit()
    return {"a": a, "b": b, "ka": ka, "kb": kb}


def tool(name):
    """The registered coroutine behind an MCP tool, by name: in-process, past
    `_ScopedFastMCP.call_tool` (TestThroughTheHttpDoor takes the real door)."""
    mcp = mcp_server._get_agent_mcp()
    fns = getattr(mcp, "_tool_manager", None)
    assert fns is not None, "FastMCP internals moved; this test needs updating"
    t = fns._tools[name]
    return t.fn


async def verify(key: str):
    return await mcp_server.KorelyAgentKeyVerifier().verify_token(key)


class TestAnUnverifiedCallReachesNothing:
    def test_no_token_means_no_tenant(self):
        """`_uid` must raise rather than default to anything: a refusal with
        its code (audit of 17 Oct 2026), as every refusal of a tool."""
        mcp_server._agent_user_id.set(None)
        with pytest.raises(ToolError, match="^error: invalid_key, "):
            mcp_server._uid()

    def test_a_tool_without_verification_does_not_answer(self, two_tenants):
        async def go():
            mcp_server._agent_user_id.set(None)
            with pytest.raises(ToolError, match="invalid_key"):
                await tool("korely_get_facts")(user_id="shared-name")
        asyncio.run(go())

    @pytest.mark.parametrize("bad", ["", "   ", "kor_live_unknown", "not-a-key"])
    def test_a_bad_key_verifies_to_nothing(self, bad):
        assert asyncio.run(verify(bad)) is None

    def test_a_revoked_key_verifies_to_nothing(self, db, two_tenants):
        from korely_agent.db.models import AgentApiKey

        row = db.query(AgentApiKey).filter(
            AgentApiKey.tenant_id == two_tenants["a"].id).one()
        key_service.revoke_api_key(db, key_id=row.id, tenant_id=two_tenants["a"].id)
        assert asyncio.run(verify(two_tenants["ka"])) is None


class TestOneTenantCannotReachAnother:
    def test_facts_are_scoped_to_the_key(self, two_tenants):
        async def go(key):
            await verify(key)
            return await tool("korely_get_facts")(user_id="shared-name")
        alpha = asyncio.run(go(two_tenants["ka"]))
        beta = asyncio.run(go(two_tenants["kb"]))

        assert "ZZALPHAMARK" in alpha and "ZZBETAMARK" not in alpha
        assert "ZZBETAMARK" in beta and "ZZALPHAMARK" not in beta

    def test_a_past_date_is_also_scoped(self, two_tenants):
        """as_of is the moat; it must not be a way around the tenant filter."""
        async def go(key):
            await verify(key)
            return await tool("korely_get_facts")(user_id="shared-name",
                                                  as_of="2099-01-01",
                                                  include_invalidated=True)
        assert "ZZBETAMARK" not in asyncio.run(go(two_tenants["ka"]))
        assert "ZZALPHAMARK" not in asyncio.run(go(two_tenants["kb"]))

    def test_asking_for_every_end_user_is_still_one_tenant(self, two_tenants):
        """No user_id means every end user OF THIS TENANT, not every end user."""
        async def go(key):
            await verify(key)
            return await tool("korely_get_facts")()
        assert "ZZBETAMARK" not in asyncio.run(go(two_tenants["ka"]))
        assert "ZZALPHAMARK" not in asyncio.run(go(two_tenants["kb"]))

    def test_a_write_lands_under_the_key_that_made_it(self, db, two_tenants):
        from korely_agent.db.models import AgentMemory

        async def go():
            await verify(two_tenants["kb"])
            return await tool("korely_add")(content="Beta wrote this.",
                                            user_id="shared-name")
        asyncio.run(go())
        db.rollback()
        rows = (db.query(AgentMemory)
                .filter(AgentMemory.content == "Beta wrote this.").all())
        assert len(rows) == 1
        assert rows[0].tenant_id == two_tenants["b"].id, "landed under the wrong tenant"


class TestIdentityDoesNotSurviveTheRequest:
    """The ContextVar is the reason this file exists.

    If a verification leaks past the call it belongs to, the next caller inherits
    somebody else's tenant. These ask whether it does.
    """

    def test_a_second_call_does_not_inherit_the_first(self, two_tenants):
        async def go():
            await verify(two_tenants["ka"])
            first = await tool("korely_get_facts")(user_id="shared-name")
            # A new caller arrives WITHOUT verifying. It must not see Alpha.
            mcp_server._agent_user_id.set(None)
            with pytest.raises(ToolError, match="invalid_key"):
                await tool("korely_get_facts")(user_id="shared-name")
            return first
        assert "ZZALPHAMARK" in asyncio.run(go())

    def test_two_tenants_interleaved_do_not_cross(self, two_tenants):
        """Concurrent tasks, deliberately interleaved at every await point."""
        async def one(key, expect, forbid, results):
            for _ in range(8):
                await verify(key)
                await asyncio.sleep(0)          # yield: let the other task run
                out = await tool("korely_get_facts")(user_id="shared-name")
                results.append((expect in out, forbid in out))

        async def go():
            ra, rb = [], []
            await asyncio.gather(
                one(two_tenants["ka"], "ZZALPHAMARK", "ZZBETAMARK", ra),
                one(two_tenants["kb"], "ZZBETAMARK", "ZZALPHAMARK", rb),
            )
            return ra, rb

        ra, rb = asyncio.run(go())
        leaked = [r for r in ra + rb if r[1]]
        missing = [r for r in ra + rb if not r[0]]
        assert not leaked, f"{len(leaked)} calls saw the other tenant's data"
        assert not missing, f"{len(missing)} calls lost their own tenant"


class TestWhyItHolds:
    """Naming the mechanism, so moving it breaks a test instead of a customer.

    The isolation above is not the tools being careful. It is that a ContextVar
    set inside an asyncio task belongs to that task: each task starts from a
    copy of its parent's context, and a write inside one is invisible to its
    siblings. That is the entire reason two tenants can be in flight at once
    through a global variable and not see each other.

    Two things follow, and both are written down as tests rather than as a
    comment somebody will delete:

      * run a tool in a task that never verified and it gets nothing, not a
        leftover. The failure mode is a refusal, which is the survivable one.
      * move any of this to a thread pool, or set the variable anywhere other
        than inside the request's own task, and the guarantee is gone.
    """

    def test_the_tenant_is_task_local(self):
        async def go():
            seen = {}

            async def task(name, value):
                mcp_server._agent_user_id.set(value)
                await asyncio.sleep(0)          # let the other one write
                seen[name] = mcp_server._agent_user_id.get()

            await asyncio.gather(task("a", "AAA"), task("b", "BBB"))
            return seen

        seen = asyncio.run(go())
        assert seen == {"a": "AAA", "b": "BBB"}, (
            "a ContextVar write crossed between tasks: every isolation test in "
            "this file is resting on this and would now be resting on nothing"
        )

    def test_a_fresh_task_inherits_nothing_set_by_a_sibling(self):
        async def go():
            async def setter():
                mcp_server._agent_user_id.set("LEAKED")
                await asyncio.sleep(0)

            async def reader():
                await asyncio.sleep(0)
                return mcp_server._agent_user_id.get()

            _, got = await asyncio.gather(setter(), reader())
            return got

        mcp_server._agent_user_id.set(None)
        assert asyncio.run(go()) is None, "a sibling task inherited a tenant"


class TestNoTestInheritsTheOneBefore:
    """pytest runs every test in one context, so an identity a test sets
    outside a task of its own reaches the next test (audit of 17 Oct 2026:
    tests/test_memory_engine.py set the tenant and left it). tests/conftest.py
    puts every variable back to None around each test; these two, in this
    order, fail without it."""

    VARIABLES = ("_agent_user_id", "_agent_project", "_agent_scopes",
                 "_agent_key_id", "_agent_end_users")

    def test_1_a_test_that_leaves_an_identity_behind(self):
        for name in self.VARIABLES:
            getattr(mcp_server, name).set(("LEFT BEHIND",))

    def test_2_the_next_one_starts_with_none(self):
        assert {name: getattr(mcp_server, name).get() for name in self.VARIABLES} == {
            name: None for name in self.VARIABLES}


class TestThroughTheHttpDoor:
    """Two tenants over the mounted transport: each call verified by the key
    of its own request, bound by `_bind_the_caller`, in a session that only
    its key may use."""

    @staticmethod
    def _facts(c, key, sid):
        r = _call(c, key, sid, "korely_get_facts", {"user_id": "shared-name"})
        assert r.status_code == 200, r.text[:200]
        result = _payload(r)["result"]
        assert result["isError"] is False, result
        return result["content"][0]["text"]

    def test_each_tenant_reads_its_own_facts(self, client, two_tenants):
        ka, kb = two_tenants["ka"], two_tenants["kb"]
        sa, sb = _open(client, ka), _open(client, kb)
        # Alternated, so a call that kept the identity of the one before
        # would read the other tenant.
        for _ in range(3):
            alpha, beta = self._facts(client, ka, sa), self._facts(client, kb, sb)
            assert "ZZALPHAMARK" in alpha and "ZZBETAMARK" not in alpha
            assert "ZZBETAMARK" in beta and "ZZALPHAMARK" not in beta

    def test_another_tenants_key_cannot_use_the_session(self, client, two_tenants):
        sa = _open(client, two_tenants["ka"])
        r = _call(client, two_tenants["kb"], sa, "korely_get_facts", {"user_id": "shared-name"})
        assert r.status_code == 404, f"{r.status_code}: {r.text[:200]}"
        assert "ZZALPHAMARK" not in r.text

    def test_a_write_lands_under_the_tenant_of_the_key(self, db, client, two_tenants):
        from korely_agent.db.models import AgentMemory

        sb = _open(client, two_tenants["kb"])
        r = _call(client, two_tenants["kb"], sb, "korely_add",
                  {"content": "Beta wrote this over HTTP.", "user_id": "shared-name"})
        assert r.status_code == 200 and _payload(r)["result"]["isError"] is False, r.text[:200]
        db.rollback()
        rows = db.query(AgentMemory).filter(AgentMemory.content == "Beta wrote this over HTTP.").all()
        assert [m.tenant_id for m in rows] == [two_tenants["b"].id]

    def test_no_key_no_call(self, client, two_tenants):
        r = _post(client, "", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
