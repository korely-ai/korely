"""The store itself.

Every request touches this: writes, reads, search, deletion. It had no tests.

The thing worth pushing on is the namespace design, because it is subtle enough
to be got wrong quietly. An account holds memories for many end users, and
`end_user_id` has three meanings that look alike in a call:

    _ALL   no filter at all: everything the account holds
    None   exactly the shared, bot-level memory (the column IS NULL)
    "u1"   exactly that end user

Confuse the first two and a caller asking for the shared memory gets every
customer's; confuse the last two and one customer sees another's.
"""
import uuid

import pytest

from korely_agent.db.models import Tenant
from korely_agent.services.memory_engine import (
    NO_AGENT,
    AgentMemoryEngine,
    MemorySearchUnavailable,
    _ALL,
)



@pytest.fixture
def eng(db, tenant):
    return AgentMemoryEngine(db, tenant.id)


@pytest.fixture
def other(db):
    t = Tenant(id=uuid.uuid4(), name="Other")
    db.add(t)
    db.commit()
    return AgentMemoryEngine(db, t.id), t


class TestTheThreeMeaningsOfEndUser:
    def test_they_are_three_different_things(self, eng, db):
        eng.add("shared bot knowledge", end_user_id=None)
        eng.add("about alice", end_user_id="alice")
        eng.add("about bob", end_user_id="bob")
        db.commit()

        everything = eng.get_all(end_user_id=_ALL)[0]
        shared = eng.get_all(end_user_id=None)[0]
        alice = eng.get_all(end_user_id="alice")[0]

        assert len(everything) == 3
        assert [m["content"] for m in shared] == ["shared bot knowledge"]
        assert [m["content"] for m in alice] == ["about alice"]

    def test_asking_for_the_shared_memory_does_not_return_everyones(self, eng, db):
        eng.add("about alice", end_user_id="alice")
        db.commit()
        assert eng.get_all(end_user_id=None)[0] == []

    def test_the_default_is_everything_the_account_holds(self, eng, db):
        eng.add("a", end_user_id="alice")
        eng.add("b", end_user_id=None)
        db.commit()
        assert len(eng.get_all()[0]) == 2


class TestNothingCrossesTheAccount:
    def test_reads(self, eng, other, db):
        oeng, _ot = other
        mine = eng.add("mine", end_user_id="shared-name")
        oeng.add("theirs", end_user_id="shared-name")
        db.commit()

        assert [m["content"] for m in eng.get_all(end_user_id="shared-name")[0]] == ["mine"]
        assert oeng.get(str(mine.id)) is None

    def test_update(self, eng, other, db):
        oeng, _ = other
        mine = eng.add("mine", end_user_id="u1")
        db.commit()
        assert oeng.update(str(mine.id), "hijacked") is None
        db.refresh(mine)
        assert mine.content == "mine"

    def test_delete(self, eng, other, db):
        oeng, _ = other
        mine = eng.add("mine", end_user_id="u1")
        db.commit()
        assert oeng.delete(str(mine.id)) is False
        assert eng.get(str(mine.id)) is not None


class TestDeletionDropsOutOfEveryRead:
    def test_a_deleted_memory_is_gone_from_all_of_them(self, eng, db):
        m = eng.add("forget me", end_user_id="u1")
        db.commit()
        mid = str(m.id)

        assert eng.delete(mid) is True
        assert eng.get(mid) is None
        assert eng.get_all(end_user_id="u1")[0] == []
        assert eng.get_all(end_user_id=_ALL)[0] == []
        assert eng.update(mid, "back from the dead") is None

    def test_deleting_twice_reports_that_it_did_nothing(self, eng, db):
        m = eng.add("x", end_user_id="u1")
        db.commit()
        assert eng.delete(str(m.id)) is True
        assert eng.delete(str(m.id)) is False

    def test_a_nonexistent_id_is_false_not_an_exception(self, eng):
        assert eng.delete(str(uuid.uuid4())) is False
        assert eng.get(str(uuid.uuid4())) is None
        assert eng.update(str(uuid.uuid4()), "x") is None


class TestAgentNamespaces:
    def test_no_agent_is_not_the_same_as_any_agent(self, eng, db):
        eng.add("default namespace", end_user_id="u1")
        eng.add("bot A", end_user_id="u1", agent_id="bot-a")
        db.commit()

        everything = eng.get_all(end_user_id="u1")[0]
        default = eng.get_all(end_user_id="u1", agent_id=NO_AGENT)[0]
        just_a = eng.get_all(end_user_id="u1", agent_id="bot-a")[0]

        assert len(everything) == 2
        assert [m["content"] for m in default] == ["default namespace"]
        assert [m["content"] for m in just_a] == ["bot A"]


class TestPagination:
    def test_limit_offset_and_total(self, eng, db):
        for i in range(7):
            eng.add(f"memory {i}", end_user_id="u1")
        db.commit()

        rows, total = eng.get_all(end_user_id="u1", limit=3)
        assert len(rows) == 3
        assert total == 7

        second, _ = eng.get_all(end_user_id="u1", limit=3, offset=3)
        assert not ({m["id"] for m in rows} & {m["id"] for m in second})

    def test_total_counts_what_matches_not_what_is_returned(self, eng, db):
        for i in range(5):
            eng.add(f"m{i}", end_user_id="u1")
        eng.add("elsewhere", end_user_id="u2")
        db.commit()
        assert eng.get_all(end_user_id="u1", limit=2)[1] == 5


class TestSearchWhenTheresNoEmbedding:
    def test_it_refuses_rather_than_returning_nothing(self, eng, db, monkeypatch):
        """An empty result and a broken retriever must not look the same.

        Returning [] would tell the caller "you have no memories about this",
        which is a statement about their data made from a failure of ours.
        """
        eng.add("Marco lives in Milan", end_user_id="u1")
        db.commit()
        # The engine's own binding, and a check that the search called it:
        # this patched `core.llm.embed`, which the engine imported by name, and
        # passed only because the suite has no embedder (audit of 17 Oct 2026).
        asked = []
        monkeypatch.setattr("korely_agent.services.memory_engine.embed",
                            lambda text, **k: asked.append(text))
        with pytest.raises(MemorySearchUnavailable):
            eng.search("where does Marco live", end_user_id="u1")
        assert asked == ["where does Marco live"]


class TestMetadataFilter:
    def test_it_filters(self, eng, db):
        eng.add("with tag", end_user_id="u1", metadata={"source": "crm"})
        eng.add("without", end_user_id="u1", metadata={"source": "email"})
        db.commit()
        got = eng.get_all(end_user_id="u1", metadata={"source": "crm"})[0]
        assert [m["content"] for m in got] == ["with tag"]

    def test_an_unknown_key_matches_nothing_rather_than_everything(self, eng, db):
        eng.add("x", end_user_id="u1", metadata={"source": "crm"})
        db.commit()
        assert eng.get_all(end_user_id="u1", metadata={"nope": "1"})[0] == []


class TestABrokenRetrieverIsNeverSilent:
    """Three call sites, and each one used to answer differently.

    `search` returned [] when embedding was unavailable, which is the same shape
    as "nothing matches". An expired key would have turned every search into a
    confident "nothing known", with HTTP 200 on it, until somebody noticed. The
    exception to say otherwise already existed and this path did not use it.
    """

    @pytest.fixture
    def no_embedding(self, monkeypatch):
        # `memory_engine` does `from ..core.llm import embed`, which binds the
        # function by value, so patching the llm module changes nothing here.
        # Patching the wrong name is not a harmless mistake in this file: a
        # suite with no API key has no embedding anyway, so the test passes
        # either way and proves nothing. Hence `working_embedding` below, which
        # fails if the patch does not take.
        monkeypatch.setattr("korely_agent.services.memory_engine.embed",
                            lambda *a, **k: None)

    @pytest.fixture
    def working_embedding(self, monkeypatch):
        from korely_agent.core.llm import EMBEDDING_DIM

        vec = [0.1] * EMBEDDING_DIM
        monkeypatch.setattr("korely_agent.services.memory_engine.embed",
                            lambda *a, **k: vec)
        return vec

    def test_the_engine_refuses(self, eng, db, no_embedding):
        eng.add("Marco lives in Milan", end_user_id="u1")
        db.commit()
        with pytest.raises(MemorySearchUnavailable):
            eng.search("where does Marco live", end_user_id="u1")

    def test_and_does_not_refuse_when_it_works(self, eng, db, working_embedding):
        """The control. Without it the test above passes in a suite that has no
        API key regardless of the code, which is a green assertion about
        nothing."""
        eng.add("Marco lives in Milan", end_user_id="u1")
        db.commit()
        hits = eng.search("where does Marco live", end_user_id="u1")
        assert [h["content"] for h in hits] == ["Marco lives in Milan"]

    def test_context_still_answers_but_says_it_is_partial(self, db, tenant, no_embedding):
        """Propagating would be wrong: the facts are still retrievable.
        Swallowing it was also wrong, and was what happened."""
        from korely_agent.services.context import assemble_context
        from korely_agent.services.facts.service import write_fact_direct

        write_fact_direct(db, tenant.id, subject="Marco", predicate="lives_in",
                          obj="Milan", end_user_id="u1")
        db.commit()

        out = assemble_context(db, tenant.id, query="where does Marco live",
                               end_user_id="u1", token_budget=800)
        assert out["degraded"] is True
        assert "could not be retrieved" in out["context"]
        assert "Milan" in out["context"], "threw away the facts it still had"

    def test_the_recommendation_clause_reaches_every_language(self, db, tenant, working_embedding):
        """Since 2026-09-27 the instance-transfer clause is part of the note on
        every query and says "if the question asks for a recommendation": the
        reader decides, in any language. The English regex it replaces never
        fired on an Italian or Japanese question."""
        from korely_agent.services import context as ctx
        from korely_agent.services.facts.service import write_fact_direct

        write_fact_direct(db, tenant.id, subject="u1", predicate="likes",
                          obj="stand-up comedy", end_user_id="u1")
        db.commit()
        for query in ("recommend a show for tonight", "consigliami una serie per stasera",
                      "今夜のおすすめの番組は?", "where does the user live"):
            out = ctx.assemble_context(db, tenant.id, query=query,
                                       end_user_id="u1", token_budget=900)
            low = out["context"].lower()
            assert "if the question asks for a recommendation" in low, query
            assert "if the question asks for a fact, answer it from what is stated" in low, query
        assert not hasattr(ctx, "_REC_INTENT_RE")

    def test_context_is_not_marked_degraded_when_it_is_fine(self, db, tenant,
                                                            monkeypatch):
        """The mirror image, so the flag is measuring the failure and not the
        test environment: a suite with no API key has no provider, so degraded
        is the CORRECT answer there and asserting False without a stub would be
        asserting nothing."""
        from korely_agent.core.llm import EMBEDDING_DIM
        from korely_agent.services.context import assemble_context

        # The one vector of the request, for the facts and the memory search
        # (BUG-299 on the hosted product, 2026-10-01).
        monkeypatch.setattr("korely_agent.services.context.embed",
                            lambda *a, **k: [0.1] * EMBEDDING_DIM)
        out = assemble_context(db, tenant.id, query="anything",
                               end_user_id="u1", token_budget=800)
        assert out["degraded"] is False

    def test_the_mcp_tool_says_so_instead_of_returning_nothing(
        self, db, tenant, no_embedding, monkeypatch
    ):
        import asyncio

        from korely_agent.mcp import server as mcp_server

        mcp = mcp_server._get_agent_mcp()
        fn = mcp._tool_manager._tools["korely_search"].fn
        mcp_server._agent_user_id.set(str(tenant.id))
        from mcp.server.fastmcp.exceptions import ToolError

        # An MCP error (isError) since 2026-10-06, with the same words.
        with pytest.raises(ToolError) as e:
            asyncio.run(fn(query="anything", user_id="u1"))
        out = str(e.value)
        assert "unavailable" in out.lower()
        assert "not an empty result" in out.lower()
