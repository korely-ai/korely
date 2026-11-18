"""GET /v1/context split for the client's prompt cache (2026-10-01).

A provider's prompt cache reuses only an identical prefix. `context` is now
`stable` and `volatile` joined by a blank line, `stable_hash` the SHA-256 of
`stable`; the NOTE on a degraded part closes the block.

`stable` is the reader note and `volatile` the facts and memories chosen as
before. A profile-first block (the facts about the end user in `stable`, in a
fixed order) was measured, lost, and went on 2026-10-06 with its setting,
CONTEXT_PROFILE (services/context.py says why); its tests went with it.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.core.auth import key_project
from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact
from korely_agent.services import context as ctx
from korely_agent.services import keys as key_service
from korely_agent.services.memory_engine import AgentMemoryEngine, MemorySearchUnavailable


@pytest.fixture(autouse=True)
def _blocks_as_before(monkeypatch):
    """These tests pin whole blocks as they were: the line on the dates under
    the memories' heading (on since 7 Oct 2026) has its own test,
    tests/test_resolved_dates_note.py."""
    from korely_agent.services import context as _ctx

    monkeypatch.setattr(_ctx, "_RESOLVED_DATES_NOTE", False)



@pytest.fixture(autouse=True)
def _no_models(monkeypatch):
    """No model: the question gets a vector that no fact holds one near, so
    the facts for it are the most recently confirmed ones (the real fallback
    of `_rank_facts`, reported as ranked: the question was understood),
    because their ranking is not what these tests are about."""
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)


def fact(db, tenant, *, subject="maria", kind="person", predicate="likes", obj="peaches",
         end_user="maria", when=None, project=None):
    f = AgentFact(tenant_id=tenant.id, end_user_id=end_user, subject_label=subject, subject_type=kind,
                  predicate_raw=predicate, predicate_canonical=predicate, object_label=obj,
                  object_is_literal=True, confidence=0.9, project_id=project,
                  valid_from=when or datetime(2026, 3, 1, tzinfo=timezone.utc))
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def memories(*texts):
    rows = [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, t)), "content": t} for t in texts]
    return lambda self, q, **k: list(rows)


def context(db, tenant, **kw):
    kw.setdefault("end_user_id", "maria")
    kw.setdefault("token_budget", 800)
    return ctx.assemble_context(db, tenant.id, query=kw.pop("query", "what does maria like?"), **kw)


def _down(self, *a, **k):
    raise MemorySearchUnavailable("down")


@pytest.fixture
def api(db, tenant):
    """A client on the real app with this test's session, and a key."""
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    row, full = key_service.create_api_key(db, tenant_id=tenant.id, name=f"k_{uuid.uuid4().hex[:6]}")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c, {"Authorization": f"Bearer {full}"}, key_project(db, row)
    app.dependency_overrides.clear()


class TestByDefault:
    """The facts chosen as before (the _FACTS_LIMIT most relevant, here by
    date), under the two headings of before; `stable` is the reader note."""

    def test_two_calls_the_same_block(self, db, tenant, monkeypatch):
        fact(db, tenant)
        fact(db, tenant, predicate="lives_in", obj="Milan")
        monkeypatch.setattr(AgentMemoryEngine, "search", memories("Maria called on Monday.", "Maria moved in May."))
        assert context(db, tenant) == context(db, tenant)

    def test_the_shape(self, db, tenant, monkeypatch):
        fact(db, tenant)
        fact(db, tenant, predicate="lives_in", obj="Milan",
             when=datetime(2026, 3, 5, tzinfo=timezone.utc))
        monkeypatch.setattr(AgentMemoryEngine, "search", memories("Maria called on Monday."))
        out = context(db, tenant)
        assert out["stable"] == ctx._READER_GUIDANCE
        assert out["volatile"] == (
            "## Known facts\n"
            "- The user lives_in Milan (since 2026-03-05)\n"
            "- The user likes peaches (since 2026-03-01)\n\n"
            "## Relevant memories\n"
            "- Maria called on Monday."
        )
        assert out["context"] == out["stable"] + "\n\n" + out["volatile"]
        assert out["stable_hash"] == hashlib.sha256(ctx._READER_GUIDANCE.encode("utf-8")).hexdigest()
        assert "## Profile" not in out["context"]
        assert out["tokens"] == len(out["context"]) // 4 <= 800

    def test_the_facts_are_the_ones_of_before(self, db, tenant, monkeypatch):
        # 15 facts about the end user: the 10 the ranking gives, in its order,
        # not the 15 a profile would list.
        for i in range(15):
            fact(db, tenant, obj=f"fruit {i:02d}", when=datetime(2026, 3, 1 + i, tzinfo=timezone.utc))
        monkeypatch.setattr(AgentMemoryEngine, "search", memories())
        expected, _ = ctx._rank_facts(db, tenant.id, "maria", None, "x", ctx._FACTS_LIMIT)
        out = context(db, tenant, token_budget=4000)
        assert out["sources"] == [ctx.fact_public_id(f.id) for f in expected]
        assert len(out["sources"]) == ctx._FACTS_LIMIT == 10

    def test_stable_is_the_note_whatever_the_question(self, db, tenant, monkeypatch):
        fact(db, tenant)
        monkeypatch.setattr(AgentMemoryEngine, "search", memories("Maria called on Monday."))
        a = context(db, tenant, query="where does maria live?")
        monkeypatch.setattr(AgentMemoryEngine, "search", memories("Acme opened an office in Turin."))
        b = context(db, tenant, query="tell me about Acme")
        assert a["stable"] == b["stable"] == ctx._READER_GUIDANCE
        assert a["stable_hash"] == b["stable_hash"] and a["volatile"] != b["volatile"]

    def test_one_more_memory_changes_only_where_it_lands(self, db, tenant, monkeypatch):
        fact(db, tenant)
        before_m, after_m, new = "Maria called on Monday.", "Maria moved in May.", "Maria asked for a refund."
        monkeypatch.setattr(AgentMemoryEngine, "search", memories(before_m, after_m))
        before = context(db, tenant)
        monkeypatch.setattr(AgentMemoryEngine, "search", memories(before_m, new, after_m))
        after = context(db, tenant)
        assert after["stable"] == before["stable"]
        assert after["volatile"].replace(f"\n- {new}", "") == before["volatile"]
        assert context(db, tenant) == after  # two calls, the same block

    def test_the_degraded_note_closes(self, db, tenant, monkeypatch):
        fact(db, tenant)
        monkeypatch.setattr(AgentMemoryEngine, "search", _down)
        out = context(db, tenant)
        assert out["degraded_parts"] == ["memories"]
        assert out["context"] == out["stable"] + "\n\n" + out["volatile"]
        assert out["volatile"].startswith("## Known facts\n")
        assert out["volatile"].split("\n\n")[-1].startswith("NOTE:")
        assert out["stable"] == ctx._READER_GUIDANCE

    def test_empty_stays_empty(self, db, tenant, monkeypatch):
        monkeypatch.setattr(AgentMemoryEngine, "search", memories())
        out = context(db, tenant, end_user_id="nobody")
        assert out["context"] == out["stable"] == out["volatile"] == ""

    def test_below_400_no_note_and_stable_is_empty(self, db, tenant, monkeypatch):
        fact(db, tenant)
        monkeypatch.setattr(AgentMemoryEngine, "search", memories())
        out = context(db, tenant, token_budget=120)
        assert out["stable"] == "" and out["context"] == out["volatile"]
        assert out["volatile"].startswith("## Known facts\n")

    def test_the_route(self, db, tenant, api, monkeypatch):
        client, h, project = api
        fact(db, tenant, project=project)
        monkeypatch.setattr(AgentMemoryEngine, "search", memories("Maria called on Monday."))
        body = client.get("/v1/context", params={"query": "what does maria like?", "user_id": "maria"},
                          headers=h).json()
        assert body["context"] == body["stable"] + "\n\n" + body["volatile"]
        assert body["stable"] == ctx._READER_GUIDANCE and "## Known facts" in body["volatile"]
        assert "## Profile" not in body["context"]
        assert client.get("/v1/context", params={"query": "something else", "user_id": "maria"},
                          headers=h).json()["stable_hash"] == body["stable_hash"]


class TestTheBudget:
    @pytest.mark.parametrize("budget", [50, 51, 60, 80, 120, 200, 399, 400, 401, 450, 800, 2000])
    def test_headings_and_newlines_count(self, db, tenant, monkeypatch, budget):
        for i in range(25):
            fact(db, tenant, obj=f"thing number {i} with some padding text")
            fact(db, tenant, subject="Acme", kind="organization", predicate="based_in", obj=f"office {i}")
        monkeypatch.setattr(AgentMemoryEngine, "search",
                            memories(*[f"Memory {i}: " + "Maria asked about the plan again. " * 6
                                       for i in range(15)]))
        out = context(db, tenant, token_budget=budget)
        assert out["tokens"] <= budget
        assert out["tokens"] == len(out["context"]) // 4
        assert out["context"] == "\n\n".join(p for p in (out["stable"], out["volatile"]) if p)

    @pytest.mark.parametrize("budget", [50, 60, 120, 400, 800])
    def test_a_degraded_block_stays_under_the_budget(self, db, tenant, monkeypatch, budget):
        """The NOTE is counted like everything else: it was prepended after the
        packing, and a 60-token budget came back with 90."""
        for i in range(25):
            fact(db, tenant, obj=f"thing number {i} with some padding text")
        monkeypatch.setattr(ctx, "_rank_facts", lambda *a, **k: (
            ctx.facts_base(db, tenant.id, "maria", None).limit(10).all(), False))
        monkeypatch.setattr(AgentMemoryEngine, "search", _down)
        out = context(db, tenant, token_budget=budget)
        assert out["degraded_parts"] == ["facts", "memories"]
        assert out["tokens"] <= budget
        # With the memories down no memory follows the facts, so they take the
        # whole room the notes leave (`_Block.facts_limit`): at every budget
        # here at least one fact is in the block, next to the NOTE. Until
        # 3 Nov 2026 the reader note took 723 of the facts' 800 characters at
        # 400 tokens, and the block could come back empty.
        assert "thing number" in out["context"]
        assert out["volatile"].split("\n\n")[-1].startswith("NOTE:")


class TestTheDegradedDoesNotEatTheFacts:
    """The NOTE on missing memories does not leave the block without its
    facts (found porting this code, 2026-10-01; the hosted product's
    955e222): counted against the facts' quota, it left no room for one fact
    under ~550 tokens and the degraded block came back empty. Since 3 Nov
    2026 what is packed before the facts is paid by both shares, and with
    the memories down no memory follows, so the facts take the whole room
    the notes leave (`_Block.facts_limit`)."""

    @pytest.mark.parametrize("budget", [400, 450, 500, 550, 600, 800])
    def test_with_the_memories_down_the_facts_are_there(self, db, tenant, monkeypatch, budget):
        for i in range(12):
            fact(db, tenant, obj=f"thing {i}")
        monkeypatch.setattr(AgentMemoryEngine, "search", _down)
        out = context(db, tenant, token_budget=budget)
        assert out["degraded_parts"] == ["memories"]
        assert out["tokens"] <= budget
        assert "thing" in out["context"]
        assert out["context"].split("\n\n")[-1].startswith("NOTE:")

    def test_at_800_as_many_facts_as_without_degradation(self, db, tenant, monkeypatch):
        for i in range(25):
            fact(db, tenant, obj=f"thing {i} described at some length to fill the share")
        monkeypatch.setattr(AgentMemoryEngine, "search", memories())
        healthy = context(db, tenant)["context"].count("thing ")
        assert healthy >= 5
        monkeypatch.setattr(AgentMemoryEngine, "search", _down)
        assert context(db, tenant)["context"].count("thing ") == healthy
